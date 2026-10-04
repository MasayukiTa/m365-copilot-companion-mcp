<#
.SYNOPSIS
  Keeps the MCP server and the Dev Tunnel host alive.

.DESCRIPTION
  Two failure modes are handled:
    1. The MCP server process dies        -> port 8000 stops responding -> restart it
    2. The Dev Tunnel host *silently drops* -> the devtunnel process stays alive but
       "Host connections" falls to 0 -> kill the stale host and re-host.
    And one it must NOT fix by re-hosting: another PC hosting this PC's tunnel (connections >= 1,
    no host of ours) -> reported in the log and .fleet\tunnel_host.json; see
    Resolve-TunnelHostingState.

  Checking only whether the processes exist is NOT enough -- the tunnel host can be a
  live process with zero relay connections, which is exactly the state that breaks
  Copilot Studio. This script polls the actual connection counts.

  Run it once and leave it; it loops forever. Register it in Task Scheduler at logon
  (see register-supervisor.ps1 / README) so it survives reboots and sleep/wake.

.PARAMETER TunnelName
  The Dev Tunnel id to host (e.g. "m365-copilot-companion").

.PARAMETER Port
  The local port the MCP server listens on.

.PARAMETER IntervalSeconds
  Health-check interval.
#>
param(
    # Empty -> resolved below from .env's MCP_TUNNEL_NAME (written by setup_devtunnel.ps1), falling
    # back to the generic "m365-copilot-companion". DO NOT hardcode a machine-specific tunnel id here:
    # a wrong default once KILLED a live tunnel host while "fixing" one that didn't exist (2026-06-12),
    # and it also leaked one user's tunnel name as the default for everyone else's fresh install.
    [string]$TunnelName = "",
    [int]$Port = 8000,
    [int]$IntervalSeconds = 15,
    # Consecutive failed checks required before acting. Debounce avoids killing a
    # healthy tunnel on a single transient "devtunnel show" glitch (false positive),
    # which would itself cause an outage. Raised 2 -> 4 (2026-06-13): tool bodies now
    # run off the event loop, so the loop should never stall, but a higher debounce is
    # cheap insurance against a single slow /health response triggering a needless kill
    # that would tear down a live Copilot MCP session.
    [int]$FailuresBeforeAction = 4,
    # How long a main.py of ours may be alive-but-not-listening before its failures start
    # counting. It is not a guess at startup time so much as a ceiling on how long we are
    # willing to wait for one: imports alone measured 10-25s, and the whole boot is longer
    # under load. Only ever applies while such a process is actually alive.
    [int]$StartupGraceSeconds = 180,
    # Dry-run the fleet coordinator auto-resume check only: log what WOULD happen
    # (marker found, pid dead, would relaunch with these args) without actually
    # starting a process. Used for verification -- never triggers a real relaunch.
    [switch]$FleetResumeDryRun,
    # No longer needed: the per-cycle resume check (after the reap) is governed by the operator's
    # `fleet_auto_resume` setting (cockpit gear popup, Recovery; default on). This switch only
    # forces it on over a setting of off. MCP_FLEET_AUTORESUME, when set, beats both.
    [switch]$FleetCycleResumeLive
)

$ErrorActionPreference = "SilentlyContinue"
# This script lives in <repo>\scripts. $Root is the REPO ROOT: .env, .venv and main.py
# (which this hosts) all live there.
$Root = Split-Path -Parent $PSScriptRoot


function Get-EnvBridgePort {
    # start_bridge.ps1 supports MCP_BRIDGE_PORT; the lifecycle supervisor must ask the same port.
    # Task Scheduler does not necessarily inherit the user's shell environment, so .env is the
    # authoritative fallback just as it is for MCP_TUNNEL_NAME below.
    $raw = [string]$env:MCP_BRIDGE_PORT
    if (-not $raw) {
        try {
            $envp = Join-Path $Root ".env"
            if (Test-Path $envp) {
                $m = (Get-Content $envp | Where-Object { $_ -match '^\s*MCP_BRIDGE_PORT\s*=' } | Select-Object -First 1)
                if ($m) { $raw = ($m -replace '^\s*MCP_BRIDGE_PORT\s*=\s*', '').Trim() }
            }
        } catch { }
    }
    $p = 0
    if ($raw -and [int]::TryParse($raw, [ref]$p) -and $p -ge 1 -and $p -le 65535) { return $p }
    return 8765
}
$BridgePort = Get-EnvBridgePort

# Shared PURE helpers (Get-BareTunnelName / Test-SupervisorTunnelDrift) used below to
# self-correct if .env's MCP_TUNNEL_NAME changes while this supervisor is already
# running -- see tunnel_name_util.ps1's header comment. No top-level side effects, so
# dot-sourcing it here is safe.
. (Join-Path $PSScriptRoot "tunnel_name_util.ps1")

function Get-EnvTunnelName {
    # Reads MCP_TUNNEL_NAME fresh from .env. Used both to resolve the STARTUP default
    # below (when -TunnelName was not passed) and, every main-loop iteration, to detect
    # a LIVE .env change (e.g. heal_tunnel.ps1's self-heal repointing .env to this
    # account's own tunnel after this supervisor already started hosting a borrowed
    # one) -- so a later .env change is picked up without requiring an external restart.
    try {
        $envp = Join-Path $Root ".env"
        if (Test-Path $envp) {
            $m = (Get-Content $envp | Where-Object { $_ -match '^\s*MCP_TUNNEL_NAME\s*=' } | Select-Object -First 1)
            if ($m) { return ($m -replace '^\s*MCP_TUNNEL_NAME\s*=\s*', '').Trim() }
        }
    } catch { }
    return ""
}

# Resolve the tunnel name: explicit -TunnelName wins; else .env's MCP_TUNNEL_NAME (set by
# setup_devtunnel.ps1 to this machine's actual tunnel); else the generic default. This keeps the
# supervisor machine-agnostic -- every install hosts ITS OWN tunnel, not a hardcoded one. This is
# only the STARTUP default -- the main loop below re-reads .env every cycle and switches live if
# it changes, so -TunnelName does not pin the supervisor to a name forever.
if (-not $TunnelName) {
    $TunnelName = Get-EnvTunnelName
}
if (-not $TunnelName) { $TunnelName = "m365-copilot-companion" }

$Log = Join-Path $env:TEMP "m365-companion-supervisor.log"

# A SUPERVISOR THAT CANNOT RUN PYTHON MUST NOT KEEP TRYING. Falling back to bare `python` was
# meant as a safety net, but on a machine with no .venv it usually resolves to the App Execution
# Alias under WindowsApps -- which is not an interpreter; run with arguments it returns an error
# code. The loop would then fail identically twice a pass, every fifteen seconds, forever, while
# reporting itself as running. Saying it once and stopping is better than doing nothing loudly.
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
$Py = $VenvPy
if (-not (Test-Path $Py)) {
    $fallback = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
    $why = ""
    if (-not $fallback) {
        $why = "there is no .venv in this checkout and no python on PATH"
    } elseif ($fallback.Source -match '\\WindowsApps\\') {
        $why = ("there is no .venv in this checkout, and 'python' on PATH is the Windows Store " +
                "App Execution Alias (" + $fallback.Source + "), which is not an interpreter")
    }
    if ($why) {
        $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
        $msg = ("$stamp  REFUSING TO RUN: $why. Run setup.bat (or quickstart.bat) to create " +
                "the .venv, then start the stack again. Exiting rather than retrying this " +
                "every 15 seconds.")
        try { Add-Content -Path $Log -Value $msg -Encoding UTF8 } catch { }
        Write-Host $msg
        exit 3
    }
    $Py = $fallback.Source
}

# Prefer the winget-installed devtunnel (kept current) over an older copy that may
# be earlier on PATH (e.g. an IT-deployed one in System32). Older host builds drop
# their relay connection much more often. Falls back to "devtunnel" on PATH.
$DevTunnel = "devtunnel"
$wingetDt = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\devtunnel.exe"
# AND THE DIRECT DOWNLOAD. setup_devtunnel.ps1 falls back to
# %LOCALAPPDATA%\devtunnel\devtunnel.exe when winget is unavailable and appends that
# directory to the USER PATH -- which the already-running cmd session that launched this
# script cannot see. Looking only at the winget path and PATH means the CLI is installed
# and unfindable, on precisely the locked-down machines that needed the fallback.
if (-not (Test-Path $wingetDt)) {
    $directDt = Join-Path $env:LOCALAPPDATA "devtunnel\devtunnel.exe"
    if (Test-Path $directDt) { $wingetDt = $directDt }
}
if (Test-Path $wingetDt) { $DevTunnel = $wingetDt }

# Single-instance guard: if another supervisor already holds the mutex, exit quietly.
# This makes it safe for both a manually-started instance and a Task Scheduler instance
# to be launched without racing each other to restart the tunnel.
$createdNew = $false
$mutex = New-Object System.Threading.Mutex($true, "Global\m365-copilot-companion-supervisor", [ref]$createdNew)
if (-not $createdNew) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  another supervisor already running -> exiting" |
        Out-File -FilePath $Log -Append -Encoding utf8
    return
}

# Bind planned-restart telemetry to THIS supervisor instance, not merely its reusable PID.
# Windows can recycle a PID after this process dies; a stale marker must not keep a real outage
# amber just because an unrelated process later receives the same number.
try {
    $selfProc = Get-Process -Id $PID -ErrorAction Stop
    $SupervisorStartedUnix = [DateTimeOffset]::new($selfProc.StartTime.ToUniversalTime()).ToUnixTimeSeconds()
} catch {
    # Ownership evidence is mandatory for new markers. If process birth cannot be measured,
    # leave zero so the reader fails closed instead of trusting an unverifiable marker.
    $SupervisorStartedUnix = 0
}

function Write-Log($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg" | Out-File -FilePath $Log -Append -Encoding utf8
}

# -- STALE CODE: A SUPERVISOR RUNS THE SCRIPT IT WAS STARTED WITH ---------------------------------
# A PowerShell script is parsed once, when its process starts. Updating this file (or the script it
# dot-sources) on disk changes NOTHING until the process is replaced. 2026-10-04: this supervisor kept
# running its pre-#122 text after the auto-resume change was merged, so when the coordinator died it
# logged "DRY RUN -- not relaunching" and nothing resumed. The server, bridge and coordinator pick up
# new code on their own restarts and the cockpit is rebuilt by rebuild_ui.ps1; only this had no
# staleness handling at all.
#
# WHAT THIS DOES. Right here, as early as possible (what is fingerprinted should be what was loaded),
# it records sha256 of this script and the script it dot-sources, plus the git HEAD read from the
# .git files (never by running git). Every tick, Invoke-SupervisorCodeCycle compares: one stat per
# file (mtime + length), a hash only when the stat moved. Differences are published to
# .fleet\supervisor_state.json (the cockpit's Recovery section reads it) and logged once.
#
# SELF-RESTART (setting supervisor_self_restart, default on, read only once the code is stale) hands
# the job to scripts\supervisor_handoff.ps1, which waits for THIS process to exit and starts the new
# supervisor, retrying. The old one exits only after the helper is confirmed alive, so a failed
# helper launch leaves this supervisor running. It happens only when Get-SupervisorRestartVerdict says
# "ok": nothing in flight (no coordinator, no pending or launching resume, no review / local-loop run,
# bridge idle), the new scripts parse, and the previous self-restart is more than 10 minutes old.
# The server and the tunnel host are separate processes: they keep running through the swap and the
# new supervisor adopts them (it launches the server only when nothing listens).
$SupFleetDir = Join-Path $Root ".fleet"
$SupervisorStatePath = Join-Path $SupFleetDir "supervisor_state.json"
$SupervisorRestartMarkPath = Join-Path $SupFleetDir "supervisor_selfrestart.json"
$SelfRestartMinIntervalSeconds = 600
# Must equal relay/code_staleness.py SUPERVISOR_CODE_FILES (pinned by a test).
$script:SupCodeFiles = @("scripts/supervisor.ps1", "scripts/tunnel_name_util.ps1")
$script:SupCodeRecorded = @{}
$script:SupCodeStat = @{}
$script:SupCodeCurrent = @{}
$script:SupCodeChanged = @()
$script:SupGitHead = ""
$script:SupExported = $null
$script:SupNoted = @{}
$script:SupLastVerdict = ""

function Get-SupervisorFileStat([string]$Rel) {
    try {
        $i = Get-Item -LiteralPath (Join-Path $Root $Rel) -ErrorAction Stop
        return ("{0}:{1}" -f $i.LastWriteTimeUtc.Ticks, $i.Length)
    } catch { return "" }
}

function Get-SupervisorFileHash([string]$Rel) {
    try {
        return (Get-FileHash -LiteralPath (Join-Path $Root $Rel) -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
    } catch { return "" }
}

function Get-SupervisorGitHead {
    # The commit the checkout is on, read from .git files; "" when it cannot be told. Informational
    # only (a commit that touched no supervisor file does not make it stale). Never runs git.
    try {
        $gitDir = Join-Path $Root ".git"
        if (Test-Path -LiteralPath $gitDir -PathType Leaf) {
            $t = (Get-Content -LiteralPath $gitDir -Raw).Trim()
            if ($t -match '^gitdir:\s*(.+)$') { $gitDir = $matches[1].Trim() }
        }
        $head = (Get-Content -LiteralPath (Join-Path $gitDir "HEAD") -Raw).Trim()
        if ($head -notmatch '^ref:\s*(.+)$') { return $head }
        $ref = $matches[1].Trim()
        $common = $gitDir
        $cf = Join-Path $gitDir "commondir"
        if (Test-Path -LiteralPath $cf) {
            $common = [IO.Path]::GetFullPath((Join-Path $gitDir (Get-Content -LiteralPath $cf -Raw).Trim()))
        }
        foreach ($d in @($gitDir, $common)) {
            $rp = Join-Path $d $ref
            if (Test-Path -LiteralPath $rp) { return (Get-Content -LiteralPath $rp -Raw).Trim() }
        }
        $pk = Join-Path $common "packed-refs"
        if (Test-Path -LiteralPath $pk) {
            foreach ($ln in (Get-Content -LiteralPath $pk)) {
                if ($ln -match ('^([0-9a-f]{40}) ' + [regex]::Escape($ref) + '$')) { return $matches[1] }
            }
        }
        return ""
    } catch { return "" }
}

function Initialize-SupervisorCodeWatch {
    # The stat is taken BEFORE the hash, so a write landing between the two is seen as a change on
    # the next check instead of being absorbed into the record.
    foreach ($r in $script:SupCodeFiles) {
        $script:SupCodeStat[$r] = Get-SupervisorFileStat $r
        $h = Get-SupervisorFileHash $r
        $script:SupCodeRecorded[$r] = $h
        $script:SupCodeCurrent[$r] = $h
    }
    $script:SupGitHead = Get-SupervisorGitHead
}

function Update-SupervisorCodeState {
    # Which recorded files differ from the disk now. mtime/length first; hash only when it moved.
    $changed = @()
    foreach ($r in $script:SupCodeFiles) {
        $st = Get-SupervisorFileStat $r
        if ($st -ne $script:SupCodeStat[$r]) {
            $script:SupCodeStat[$r] = $st
            $script:SupCodeCurrent[$r] = Get-SupervisorFileHash $r
        }
        if ($script:SupCodeCurrent[$r] -ne $script:SupCodeRecorded[$r]) { $changed += $r }
    }
    $script:SupCodeChanged = @($changed)
}

function Write-SupervisorState {
    # Atomic. Advisory for the cockpit, which also checks that pid is alive; never blocks anything.
    param([string]$Verdict = "")
    try {
        if (-not (Test-Path $SupFleetDir)) { New-Item -ItemType Directory -Path $SupFleetDir -Force | Out-Null }
        $fp = [ordered]@{}
        foreach ($r in $script:SupCodeFiles) { $fp[$r] = [string]$script:SupCodeRecorded[$r] }
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $body = [ordered]@{
            pid = $PID
            start_ts = $SupervisorStartedUnix
            fingerprint = $fp
            git_head = $script:SupGitHead
            checked = $now
            supervisor = [ordered]@{
                pid = $PID
                started = $SupervisorStartedUnix
                stale = [bool]($script:SupCodeChanged.Count -gt 0)
                changed_files = @($script:SupCodeChanged)
            }
            self_restart = [ordered]@{ verdict = $Verdict; ts = $now }
        } | ConvertTo-Json -Depth 6 -Compress
        $tmp = $SupervisorStatePath + ".tmp." + $PID
        [IO.File]::WriteAllText($tmp, $body, (New-Object Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $tmp -Destination $SupervisorStatePath -Force
    } catch { }
}

function Get-SupervisorSelfRestartSetting {
    # The `supervisor_self_restart` setting, "on" or "off", through the product's own reader. A
    # reader that cannot run reads as the registry default, ON. Asked only once the code is stale.
    $setting = ""
    try {
        $setting = ([string](& $Py -c "import sys; sys.path.insert(0, r'$Root'); from relay import code_staleness; print(code_staleness.self_restart_setting())" 2>$null)).Trim()
    } catch { $setting = "" }
    if ($setting -eq "off") { return "off" }
    return "on"
}

function Test-SupervisorScriptsParse {
    # Do the files the NEXT supervisor will run parse? Same parser PowerShell itself uses to load
    # them, in-process. Returns the errors (empty = parses). A file that cannot be read is an error.
    param([string[]]$Paths)
    $errs = @()
    foreach ($p in $Paths) {
        try {
            $tokens = $null; $perr = $null
            [void][System.Management.Automation.Language.Parser]::ParseFile($p, [ref]$tokens, [ref]$perr)
            if ($perr -and $perr.Count -gt 0) {
                $errs += ("{0}: {1} (line {2})" -f (Split-Path $p -Leaf), $perr[0].Message, $perr[0].Extent.StartLineNumber)
            }
        } catch { $errs += ("{0}: unreadable ({1})" -f (Split-Path $p -Leaf), $_.Exception.Message) }
    }
    return $errs
}

function Get-SupervisorRestartVerdict {
    # PURE. "ok" -- or the FIRST reason the supervisor must not replace itself now. An unknown
    # state is a refusal (bridge "unknown" counts as busy), never a licence.
    param(
        [bool]$Stale,
        [string]$Setting,
        [bool]$ParseOk,
        [double]$LastRestartAgeSeconds = -1,
        [double]$MinIntervalSeconds = 600,
        [int]$CoordinatorCount = 0,
        [bool]$SnapshotPending = $false,
        [bool]$ResumeInProgress = $false,
        [bool]$OtherRunActive = $false,
        [string]$Bridge = "idle"
    )
    if (-not $Stale) { return "not_stale" }
    if ($Setting -eq "off") { return "setting_off" }
    if (-not $ParseOk) { return "parse_error" }
    if ($LastRestartAgeSeconds -ge 0 -and $LastRestartAgeSeconds -lt $MinIntervalSeconds) { return "loop_guard" }
    if ($CoordinatorCount -gt 0) { return "coordinator_running" }
    if ($SnapshotPending) { return "snapshot_pending" }
    if ($ResumeInProgress) { return "resume_in_progress" }
    if ($OtherRunActive) { return "run_active" }
    if ($Bridge -ne "idle") { return "bridge_busy" }
    return "ok"
}

function Get-SupervisorBridgeState {
    # "idle" | "busy" | "unknown". Same reading as Invoke-StaleServerCycle: turn_running / busy from
    # /status; an unreadable answer is "unknown" unless the port itself proves no bridge of ours is
    # there (nothing listening, or a foreign process).
    try {
        $breq = [System.Net.WebRequest]::Create("http://127.0.0.1:$BridgePort/status")
        $breq.Method = "GET"; $breq.Timeout = 5000; $breq.ReadWriteTimeout = 5000
        $bresp = $breq.GetResponse()
        $bbody = (New-Object System.IO.StreamReader($bresp.GetResponseStream())).ReadToEnd()
        $bresp.Close()
        $bj = $bbody | ConvertFrom-Json -ErrorAction Stop
        if (-not $bj -or $bj.ok -ne $true) { throw "bridge /status did not return bridge JSON" }
        if (($bj.turn_running -eq $true) -or ($bj.busy -eq $true)) { return "busy" }
        return "idle"
    } catch {
        try {
            $bv = Get-BridgePortVerdict $BridgePort
            if ($bv.Kind -eq "foreign" -or $bv.Kind -eq "none") { return "idle" }
        } catch { }
        return "unknown"
    }
}

function Test-SupervisorResumeInProgress {
    # A resume this supervisor launched that has not exited, or a launch guard that is still fresh
    # (.fleet\resume_launch.json, written BEFORE Start-Process by Invoke-FleetAutoResume).
    try {
        if ($script:AutoResumeRunners -and $script:AutoResumeRunners.Count -gt 0) { return $true }
        $gp = Join-Path $SupFleetDir "resume_launch.json"
        if (Test-Path -LiteralPath $gp) {
            $g = (Get-Content -LiteralPath $gp -Raw -ErrorAction Stop) | ConvertFrom-Json -ErrorAction Stop
            $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$g.ts
            if ($age -ge 0 -and $age -lt 600) {
                $gpid = 0
                try { $gpid = [int]$g.pid } catch { $gpid = 0 }
                if ($gpid -eq 0 -or (Get-Process -Id $gpid -ErrorAction SilentlyContinue)) { return $true }
            }
        }
        return $false
    } catch { return $true }   # cannot tell -> treat as in progress
}

function Test-SupervisorOtherRunActive {
    # A review run or a LOCAL_LOOP job whose process is alive.
    try {
        $rm = Get-ReviewActiveMarker
        if ($null -ne $rm -and (Test-ReviewMarkerProcessAlive $rm)) { return $true }
        if (Test-Path -LiteralPath $LocalLoopMarkerDir) {
            foreach ($f in @(Get-ChildItem -Path $LocalLoopMarkerDir -Filter "*.json" -File -ErrorAction SilentlyContinue)) {
                try { $m = (Get-Content -Path $f.FullName -Raw -ErrorAction Stop) | ConvertFrom-Json -ErrorAction Stop } catch { continue }
                if ($null -ne $m -and (Test-LocalLoopMarkerProcessAlive $m)) { return $true }
            }
        }
        return $false
    } catch { return $true }
}

function Get-SupervisorLastRestartAge {
    # Seconds since the previous self-restart, -1 when there was none / the mark is unreadable.
    try {
        if (-not (Test-Path -LiteralPath $SupervisorRestartMarkPath)) { return -1 }
        $m = (Get-Content -LiteralPath $SupervisorRestartMarkPath -Raw -ErrorAction Stop) | ConvertFrom-Json -ErrorAction Stop
        $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$m.ts
        if ($age -lt 0) { return 0 }
        return [double]$age
    } catch { return -1 }
}

function Start-SupervisorHandoff {
    # Starts scripts\supervisor_handoff.ps1 (waits for THIS process to exit, then starts the new
    # supervisor and retries). $true only when the helper is confirmed alive after a moment; the
    # caller exits only then.
    try {
        $helper = Join-Path $PSScriptRoot "supervisor_handoff.ps1"
        if (-not (Test-Path -LiteralPath $helper)) { return $false }
        $a = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", ('"{0}"' -f $helper),
               "-OldPid", $PID, "-TunnelName", ('"{0}"' -f $TunnelName), "-Port", $Port,
               "-IntervalSeconds", $IntervalSeconds, "-FailuresBeforeAction", $FailuresBeforeAction,
               "-StartupGraceSeconds", $StartupGraceSeconds, "-LogPath", ('"{0}"' -f $Log))
        if ($FleetResumeDryRun) { $a += "-FleetResumeDryRun" }
        if ($FleetCycleResumeLive) { $a += "-FleetCycleResumeLive" }
        $p = Start-Process -FilePath (Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe") `
                -ArgumentList $a -WindowStyle Hidden -PassThru
        if (-not $p) { return $false }
        Start-Sleep -Seconds 2
        $p.Refresh()
        return (-not $p.HasExited)
    } catch { return $false }
}

function Invoke-SupervisorCodeCycle {
    # Once per tick. Cheap when nothing changed (a stat per file). Never throws.
    try {
        Update-SupervisorCodeState
        $changed = @($script:SupCodeChanged)
        $stale = ($changed.Count -gt 0)
        $verdict = "not_stale"
        $setting = ""
        if ($stale) {
            $key = ($changed -join "|")
            $setting = Get-SupervisorSelfRestartSetting
            $paths = @(foreach ($r in $script:SupCodeFiles) { Join-Path $Root $r }) + @(Join-Path $PSScriptRoot "supervisor_handoff.ps1")
            $parseErrs = @(Test-SupervisorScriptsParse $paths)
            $busyArgs = @{}
            if ($setting -ne "off" -and $parseErrs.Count -eq 0) {
                $busyArgs = @{
                    CoordinatorCount = @(Get-ThisCheckoutFleetCoordinatorPids | Where-Object { $_ }).Count
                    SnapshotPending = [bool](Get-FleetPendingSnapshot)
                    ResumeInProgress = [bool](Test-SupervisorResumeInProgress)
                    OtherRunActive = [bool](Test-SupervisorOtherRunActive)
                    Bridge = (Get-SupervisorBridgeState)
                }
            }
            $verdict = Get-SupervisorRestartVerdict -Stale $true -Setting $setting -ParseOk ($parseErrs.Count -eq 0) `
                -LastRestartAgeSeconds (Get-SupervisorLastRestartAge) -MinIntervalSeconds $SelfRestartMinIntervalSeconds @busyArgs
            if (-not $script:SupNoted.ContainsKey("stale:$key")) {
                $script:SupNoted["stale:$key"] = $true
                Write-Log ("supervisor is running OLDER CODE than the checkout (changed on disk: {0}) -- a PowerShell script is loaded once, so a restart is needed; self-restart setting is {1}" -f ($changed -join ", "), $setting)
            }
            if ($verdict -ne "ok" -and -not $script:SupNoted.ContainsKey("v:${key}:$verdict")) {
                $script:SupNoted["v:${key}:$verdict"] = $true
                $why = $verdict
                if ($verdict -eq "parse_error") { $why = "the new script does not parse: " + ($parseErrs -join "; ") }
                Write-Log "supervisor self-restart not done now: $why"
            }
        }
        $exportKey = "{0}|{1}|{2}" -f $stale, ($changed -join "|"), $verdict
        if ($exportKey -ne $script:SupExported) {
            $script:SupExported = $exportKey
            Write-SupervisorState -Verdict $verdict
        }
        if ($verdict -ne "ok") { return }

        # Safe, allowed, and the new code parses. The mark is written FIRST so a restart that goes
        # wrong cannot loop: the next supervisor sees it and waits out the interval.
        if (-not (Test-Path $SupFleetDir)) { New-Item -ItemType Directory -Path $SupFleetDir -Force | Out-Null }
        $markBody = [ordered]@{ ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); from_pid = $PID; files = @($changed) } | ConvertTo-Json -Compress
        [IO.File]::WriteAllText($SupervisorRestartMarkPath, $markBody, (New-Object Text.UTF8Encoding($false)))
        Write-Log ("supervisor code changed on disk ({0}) and nothing is in flight -- handing over to a fresh supervisor so the checkout's fixes are live" -f ($changed -join ", "))
        if (Start-SupervisorHandoff) {
            Write-Log "supervisor handoff helper is running -- this supervisor exits now; the server and tunnel host keep running"
            exit 0
        }
        Write-Log "supervisor self-restart FAILED to start the handoff helper -- staying up on the old code"
    } catch {
        Write-Log "supervisor code check failed: $($_.Exception.Message)"
    }
}

Initialize-SupervisorCodeWatch
Write-SupervisorState

# The cockpit must distinguish "the server died" from "the supervisor is deliberately
# replacing stale code".  Without a machine-readable transition, both are a few seconds of
# connection refused and both render as the same red Server/Tunnel pair.  This marker is advisory
# only and intentionally short-lived on the reader side; a stale file can never mask a real outage.
$ServerTransitionPath = Join-Path (Join-Path $Root ".fleet") "server_transition.json"

function Write-ServerTransition([string]$Reason) {
    try {
        if ([string]::IsNullOrWhiteSpace($Reason)) { return }
        if (-not (Test-Path $FleetDir)) { New-Item -ItemType Directory -Path $FleetDir -Force | Out-Null }
        $tmp = $ServerTransitionPath + ".tmp." + $PID
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        # Keep the UI's planned-restart state aligned with THIS supervisor's actual policy:
        # main.py may spend StartupGraceSeconds alive-but-not-listening, then ordinary failure
        # debounce still consumes FailuresBeforeAction * IntervalSeconds before another action.
        $transitionBudgetSeconds = $StartupGraceSeconds + ($FailuresBeforeAction * $IntervalSeconds)
        if ($transitionBudgetSeconds -lt 1) { $transitionBudgetSeconds = 1 }
        $body = [ordered]@{
            state = "planned_restart"
            reason = $Reason
            started = $now
            expires = $now + $transitionBudgetSeconds
            supervisor_pid = $PID
            supervisor_started = $SupervisorStartedUnix
        } | ConvertTo-Json -Compress
        [IO.File]::WriteAllText($tmp, $body, (New-Object Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $tmp -Destination $ServerTransitionPath -Force
    } catch {
        # Health telemetry must never be able to block the restart it describes.
    }
}

function Clear-ServerTransition {
    try { Remove-Item -LiteralPath $ServerTransitionPath -Force -ErrorAction SilentlyContinue } catch { }
}

# -- WHICH PYTHON: re-decided before every launch, not once at startup (new-PC analysis D2) ----
# $Py above is resolved ONCE, when this process starts. A supervisor started before the .venv
# existed -- start_all run before quickstart, or a logon autostart that fires while setup.bat is
# still creating it -- got the PATH python and kept it for its whole life: after setup completed,
# every launch of main.py still ran on an interpreter without the server's packages and died on
# import, and nothing replaced this supervisor (start_all restarts it only on tunnel-name drift).
# Re-running quickstart, which is what doctor advised, changed nothing.
#
# SO BEFORE EACH LAUNCH (the server, the auto-resume runners, and once per tick for the reaper
# and the queue drain) ASK AGAIN. The common case is one Test-Path: the .venv interpreter is
# already the one in use. Only when a .venv interpreter has appeared that we are not using is it
# RUN once (Test-PythonRuns) -- a .venv caught half-created by setup.bat has a python.exe that
# cannot start, and switching to it would trade a working interpreter for a broken one. If it
# does not run we keep the current one, say why once, and try again after
# $script:PyRecheckSeconds. Only ever TOWARDS the .venv: when the .venv disappears (setup.bat
# rebuilding it) the current interpreter is kept, because the .venv coming back is the only
# change that fixes anything.
$script:PyRecheckSeconds = 60
$script:PyRejectedAt = $null
$script:PyRejectedWhy = $null

function Test-PythonRuns {
    # Does this file start as a Python interpreter? @{ Ok; Why }. Bounded: a hung start is a
    # failure after $TimeoutMs, not a hung supervisor.
    param([string]$Exe, [int]$TimeoutMs = 20000)
    $p = $null
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $Exe
        $psi.Arguments = '-c "import sys; print(sys.version_info[0])"'
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        $outTask = $p.StandardOutput.ReadToEndAsync()
        $errTask = $p.StandardError.ReadToEndAsync()
        if (-not $p.WaitForExit($TimeoutMs)) {
            try { $p.Kill() } catch { }
            return @{ Ok = $false; Why = "it did not finish a one-line start within $([int]($TimeoutMs / 1000))s" }
        }
        $out = [string]$outTask.Result
        $err = [string]$errTask.Result
        if ($p.ExitCode -eq 0 -and $out.Trim() -eq "3") { return @{ Ok = $true; Why = "" } }
        $first = ($err -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)
        return @{ Ok = $false; Why = ("exit code " + $p.ExitCode + $(if ($first) { ": " + $first.Trim() } else { "" })) }
    } catch {
        return @{ Ok = $false; Why = ("it could not be started: " + $_.Exception.Message) }
    } finally {
        if ($p) { try { $p.Dispose() } catch { } }
    }
}

function Update-PythonInterpreter {
    # Switches $script:Py to the .venv interpreter when one exists, is not the one in use, and
    # runs. Logs every switch and (once per distinct reason) every refusal. $Before names the
    # launch this is about to serve, for the log line.
    param([string]$Before)
    if ($script:Py -eq $script:VenvPy) { return }
    if (-not (Test-Path -LiteralPath $script:VenvPy)) { return }
    if ($script:PyRejectedAt -and ((Get-Date) - $script:PyRejectedAt).TotalSeconds -lt $script:PyRecheckSeconds) { return }
    $check = Test-PythonRuns -Exe $script:VenvPy
    if (-not $check.Ok) {
        $script:PyRejectedAt = Get-Date
        if ($script:PyRejectedWhy -ne $check.Why) {
            Write-Log ("interpreter: " + $script:VenvPy + " exists but does not run (" + $check.Why +
                       ") -- keeping " + $script:Py + ". If setup.bat is still running this clears itself; " +
                       "otherwise re-run setup.bat. Re-checked every " + $script:PyRecheckSeconds + "s.")
            $script:PyRejectedWhy = $check.Why
        }
        return
    }
    Write-Log ("interpreter: switching " + $script:Py + " -> " + $script:VenvPy + " before " + $Before +
               " (this supervisor started before the .venv existed; the .venv now exists and runs)")
    $script:Py = $script:VenvPy
    $script:PyRejectedAt = $null
    $script:PyRejectedWhy = $null
}

# -- WHO IS ON THE PORT: this checkout's main.py, or something else (new-PC analysis D13) ------
# Port 8000 was assumed to be ours. Start-Server stopped EVERY process listening on it, and
# Test-ServerUp counted ANY HTTP answer on it -- a 404, a directory listing -- as "server up".
# So another local service on :8000 was either killed every minute or mistaken for the server,
# in which case main.py was never started while doctor said the server was down.
#
# NOW A PORT IS A CLAIM, NOT AN IDENTITY, the same rule start_all's D12 fix applies
# (Get-ThisCheckoutServerProcesses): only a process whose command line is this checkout's
# main.py is ours to stop. ONE EXTRA STEP IS MEASURED, NOT ASSUMED: the process that owns the
# port is NOT the one whose command line names the checkout. Measured on this machine
# 2026-09-24: :8000 is owned by "...\Python310\python.exe main.py", whose PARENT is
# "<checkout>\.venv\Scripts\python.exe main.py" -- the .venv python.exe is a launcher that
# starts the base interpreter as a child. So a main.py owner counts as ours when its own OR its
# parent's command line names this checkout, or when it (or its parent) is the process this
# supervisor launched ($script:ServerProc; that also covers a launch on a PATH python, whose
# command line names no checkout at all).
function Get-PortListenerPids {
    # @{ Queried; Pids }. Queried=$false only when the port could not be inspected.
    # AN EMPTY ANSWER ARRIVES AS AN EXCEPTION. Get-NetTCPConnection -ErrorAction Stop THROWS
    # (CimJobException, category ObjectNotFound) when nothing matches -- measured 2026-09-24 --
    # so a plain try/catch read "nothing is listening" as "could not look". That made the
    # startup launch below ("nothing is listening on :$Port at startup") dead code: this
    # machine's supervisor log has never once printed it.
    try {
        $pids = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
                  Select-Object -ExpandProperty OwningProcess -Unique)
        return @{ Queried = $true; Pids = $pids }
    } catch {
        if ($_.CategoryInfo.Category -eq 'ObjectNotFound') { return @{ Queried = $true; Pids = @() } }
        return @{ Queried = $false; Pids = @() }
    }
}

function Test-IsMainPyCommandLine {
    # main.py as an argument or the end of a path -- not test_main.py, not main.pyc.
    param([string]$CommandLine)
    return ($CommandLine -match '(?i)(^|[\s"\\/])main\.py(["\s]|$)')
}

function Test-IsThisCheckoutServerCommandLine {
    # PURE. This checkout's main.py: main.py AND the checkout's folder followed by a separator,
    # so C:\x\repo does not claim C:\x\repo2's server.
    param([string]$CommandLine, [string]$RootDir)
    if (-not $CommandLine -or -not $RootDir) { return $false }
    if (-not (Test-IsMainPyCommandLine $CommandLine)) { return $false }
    $pat = "*" + [System.Management.Automation.WildcardPattern]::Escape($RootDir.TrimEnd('\')) + "\*"
    return ($CommandLine -like $pat)
}

function Get-ProcessVerdict {
    # Is this pid this checkout's server? @{ Ours = $true | $false | $null; Desc }. $null means
    # it cannot be told (the process is gone, or its command line is not readable -- another
    # user's or an elevated process), and a caller must never stop a process on $null.
    param([int]$ProcId)
    $tracked = 0
    if ($script:ServerProc) {
        try {
            $script:ServerProc.Refresh()
            if (-not $script:ServerProc.HasExited) { $tracked = $script:ServerProc.Id }
        } catch { }
    }
    if ($tracked -and $ProcId -eq $tracked) {
        return @{ Ours = $true; Desc = "pid $ProcId (the server this supervisor launched)" }
    }
    $p = $null
    try { $p = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcId" -ErrorAction Stop } catch {
        return @{ Ours = $null; Desc = "pid $ProcId (its details could not be read: $($_.Exception.Message))" }
    }
    if (-not $p) { return @{ Ours = $null; Desc = "pid $ProcId (no longer running)" } }
    $cl = [string]$p.CommandLine
    if (-not $cl) {
        return @{ Ours = $null; Desc = "pid $ProcId ($($p.Name); its command line cannot be read -- another user's or an elevated process)" }
    }
    $desc = "pid $ProcId ($($p.Name)): $($cl.Trim())"
    if (Test-IsThisCheckoutServerCommandLine $cl $Root) { return @{ Ours = $true; Desc = $desc } }
    if (Test-IsMainPyCommandLine $cl) {
        if ($tracked -and $p.ParentProcessId -eq $tracked) { return @{ Ours = $true; Desc = $desc } }
        try {
            $pp = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)" -ErrorAction Stop
            if ($pp -and (Test-IsThisCheckoutServerCommandLine ([string]$pp.CommandLine) $Root)) {
                return @{ Ours = $true; Desc = $desc }
            }
        } catch { }
    }
    return @{ Ours = $false; Desc = $desc }
}

function Get-HealthServerPid {
    # PURE. The server_pid main.py's /health reports, or $null when the body is not main.py's
    # answer. main.py's /health returns JSON {"status": "ok", "server_pid": <int>, ...} (see
    # health() and _server_identity() in main.py); a 200 without both is someone else's page.
    param([string]$Body)
    if (-not $Body) { return $null }
    try { $j = $Body | ConvertFrom-Json -ErrorAction Stop } catch { return $null }
    if (-not $j -or $j.status -ne "ok") { return $null }
    $sp = 0
    try { $sp = [int]$j.server_pid } catch { return $null }
    if ($sp -le 0) { return $null }
    return $sp
}

# A port held by something else is logged in full when the holder changes, and again every
# ten minutes while it stays -- not on every refused launch.
$script:ForeignHolderKey = ""
$script:ForeignHolderLoggedAt = [datetime]::MinValue

function Write-ForeignPortHolderLog {
    param([object[]]$Holders)
    $key = (@($Holders | ForEach-Object { $_.Desc }) -join " | ")
    if ($key -eq $script:ForeignHolderKey -and
        ((Get-Date) - $script:ForeignHolderLoggedAt).TotalMinutes -lt 10) { return }
    $script:ForeignHolderKey = $key
    $script:ForeignHolderLoggedAt = Get-Date
    Write-Log (":$Port is held by " + $key + " -- that is not this checkout's MCP server (" +
               (Join-Path $Root "main.py") + "), so the supervisor will NOT stop it, and it cannot " +
               "start the server while the port is taken. To fix: close that program, or move it to " +
               "another port (this server's port $Port is fixed in main.py). The server is started " +
               "automatically once :$Port is free.")
}
$script:VerifiedServerPid = 0

function Test-ServerUp {
    # A TCP-only connect check cannot detect the failure mode where the port stays
    # LISTENING but the asyncio event loop is dead (every request times out, CLOSE_WAIT
    # piles up) -- seen 2026-06-12. Issue a real HTTP GET against the dedicated /health
    # route: it is an async handler that runs directly on the event loop and does no
    # blocking work, so it answers fast even while heavy tools run (those now run in a
    # worker-thread pool). A fast 200 from /health = the loop is alive and servicing
    # requests; only a timeout / connect failure / no-response counts as down. (Older
    # builds had no /health and probed /mcp, treating any 4xx as alive; /health is a
    # cleaner liveness signal that does not depend on MCP stream-header quirks.)
    # IT USED TO COLLAPSE THREE DIFFERENT ANSWERS INTO ONE `$false`, AND THEY WANT OPPOSITE
    # TREATMENT. "Nothing is listening" means the server has not finished starting (or has
    # died) -- the right response is to wait, or to launch if it really is gone. "Listening
    # but not answering within the timeout" means a wedged event loop -- the right response is
    # to kill it. The log said only "server check failed" for both.
    #
    # 2026-09-15: a restart loop ran for nine minutes, killing a server three times. The log
    # could not say whether each kill hit a booting server or a wedged one, and those have
    # opposite fixes, so the loop could not be diagnosed from what was recorded -- only from
    # standing next to the machine. $script:LastServerCheck now carries the distinction so the
    # next occurrence names itself.
    #
    # AND AN ANSWER IS NOT PROOF OF WHO ANSWERED (D13). Any HTTP status used to count as alive,
    # so a different program on :$Port -- a dev server's 404, a directory listing -- was taken
    # for the server, and main.py was never started. Now a 200 counts only when the body is
    # main.py's /health answer (Get-HealthServerPid) AND the pid it reports is this checkout's
    # server (Get-ProcessVerdict; verified once per pid, not every tick). An HTTP ERROR still
    # counts as alive when the port's owner is this checkout's main.py -- a loop that answers
    # at all is not wedged, which is why "any answer" was accepted in the first place -- and
    # as "foreign" otherwise. A "foreign" result is what the main loop and Start-Server act on.
    $script:LastServerCheck = "unknown"
    $body = $null
    try {
        $req = [System.Net.WebRequest]::Create("http://127.0.0.1:$Port/health")
        $req.Method = "GET"
        $req.Timeout = 5000
        $req.ReadWriteTimeout = 5000
        $resp = $req.GetResponse()
        try { $body = (New-Object System.IO.StreamReader($resp.GetResponseStream())).ReadToEnd() } catch { $body = $null }
        $resp.Close()
    } catch [System.Net.WebException] {
        $r = $_.Exception.Response
        if ($r) {
            $code = 0
            try { $code = [int]$r.StatusCode } catch { }
            $r.Close()
            $listeners = Get-PortListenerPids
            $verdicts = @($listeners.Pids | ForEach-Object { Get-ProcessVerdict $_ })
            if (@($verdicts | Where-Object { $_.Ours -eq $true }).Count -gt 0) {
                $script:LastServerCheck = "answered HTTP $code (this checkout's server owns :$Port)"
                return $true
            }
            $who = (@($verdicts | ForEach-Object { $_.Desc }) -join " | ")
            if (-not $who) { $who = "a process that could not be identified" }
            $script:LastServerCheck = "foreign: HTTP $code on /health from $who"
            return $false
        }
        $script:LastServerCheck = [string]$_.Exception.Status   # Timeout / ConnectFailure / ...
        return $false
    } catch {
        $script:LastServerCheck = "threw: " + $_.Exception.GetType().Name
        return $false
    }
    $serverPid = Get-HealthServerPid $body
    if ($null -eq $serverPid) {
        $snippet = ""
        if ($body) { $snippet = ($body -replace '\s+', ' ').Trim(); if ($snippet.Length -gt 80) { $snippet = $snippet.Substring(0, 80) + "..." } }
        $script:LastServerCheck = "foreign: HTTP 200 on /health, but not main.py's answer ($snippet)"
        return $false
    }
    if ($serverPid -ne $script:VerifiedServerPid) {
        $v = Get-ProcessVerdict $serverPid
        if ($v.Ours -eq $false) {
            $script:LastServerCheck = "foreign: a main.py /health answer from $($v.Desc), which is not this checkout's server"
            return $false
        }
        # $null (cannot tell) is accepted: the answer has main.py's shape, and refusing it would
        # restart-loop a healthy server on a machine where process details are unreadable.
        if ($v.Ours -eq $true) { $script:VerifiedServerPid = $serverPid }
    }
    $script:LastServerCheck = "ok"
    return $true
}


function Test-IsThisCheckoutBridgeCommandLine {
    # PURE. The process which owns the bridge HTTP port is ours only when its command line names
    # THIS checkout's bridge/copilot_bridge.py. A random local http.server on the reserved port is
    # not a broken bridge and must not be allowed to hold an unrelated stale MCP server hostage.
    param([string]$CommandLine, [string]$RootDir)
    if (-not $CommandLine -or -not $RootDir) { return $false }
    $bridge = Join-Path $RootDir "bridge\copilot_bridge.py"
    $pat = "*" + [System.Management.Automation.WildcardPattern]::Escape($bridge) + "*"
    return ($CommandLine -like $pat)
}

function Get-BridgePortVerdict {
    # PURE-ISH observation: @{ Kind = ours|foreign|none|unknown; Desc }. `foreign` is PROOF that
    # the real bridge cannot be serving this port, so no bridge turn can be in flight there.
    # `unknown` stays fail-closed/busy.
    param([int]$BridgePort)
    $listeners = @()
    try {
        $listeners = @(Get-NetTCPConnection -LocalPort $BridgePort -State Listen -ErrorAction Stop)
    } catch {
        if ($_.CategoryInfo.Category -eq 'ObjectNotFound') {
            return @{ Kind = "none"; Desc = ":$BridgePort has no listener" }
        }
        return @{ Kind = "unknown"; Desc = ":$BridgePort listener could not be inspected: $($_.Exception.Message)" }
    }
    if ($listeners.Count -eq 0) { return @{ Kind = "none"; Desc = ":$BridgePort has no listener" } }

    $foreign = New-Object System.Collections.Generic.List[string]
    $unknown = New-Object System.Collections.Generic.List[string]
    foreach ($pid0 in @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)) {
        try { $p = Get-CimInstance Win32_Process -Filter ("ProcessId=" + [int]$pid0) -ErrorAction Stop }
        catch { $unknown.Add("pid $pid0 (unreadable)"); continue }
        if (-not $p -or -not $p.CommandLine) { $unknown.Add("pid $pid0 (no command line)"); continue }
        $cl = [string]$p.CommandLine
        if (Test-IsThisCheckoutBridgeCommandLine $cl $Root) {
            return @{ Kind = "ours"; Desc = "pid ${pid0} (this checkout's bridge)" }
        }
        $foreign.Add("pid ${pid0} ($($p.Name))")
    }
    if ($unknown.Count -gt 0) {
        return @{ Kind = "unknown"; Desc = ((@($foreign) + @($unknown)) -join " | ") }
    }
    return @{ Kind = "foreign"; Desc = ($foreign -join " | ") }
}

# A SERVER RUNNING CODE THE CHECKOUT HAS MOVED PAST, restarted by the thing that owns its
# lifecycle instead of by whoever happens to look at the panel.
#
# A running process keeps executing what it imported at startup. Every commit therefore turns
# the cockpit's server dot amber and leaves it amber until a person notices and restarts --
# three times on 2026-09-17 alone, each time after the operator pointed at it. That is the
# machine asking a human to do something the machine can decide: the server reports
# `server_code` itself (scripts/stale_server_check.classify_staleness), and whether anything is
# in flight is readable from .fleet/status.json and the bridge.
#
# ONLY WHEN NOTHING IS RUNNING. Restarting mid-run takes the tool path away from a live worker,
# which is a worse failure than stale code. Idle means: no fleet run, and the bridge reporting
# neither a turn nor busy. Anything unreadable counts as BUSY -- an unknown state must never
# authorise a restart.
#
# AND ONLY AFTER IT PERSISTS. Two consecutive checks, so a commit landing between the health
# read and the idle read does not cycle a server that was about to be used anyway.
$script:StaleStreak = 0
function Invoke-StaleServerCycle {
    try {
        $req = [System.Net.WebRequest]::Create("http://127.0.0.1:$Port/health")
        $req.Method = "GET"; $req.Timeout = 5000; $req.ReadWriteTimeout = 5000
        $resp = $req.GetResponse()
        $body = (New-Object System.IO.StreamReader($resp.GetResponseStream())).ReadToEnd()
        $resp.Close()
    } catch {
        $script:StaleStreak = 0
        return
    }
    if ($body -notmatch '"server_code"\s*:\s*"stale"') { $script:StaleStreak = 0; return }

    $busy = $true
    try {
        $statusPath = Join-Path $Root ".fleet\status.json"
        $fleetRunning = $false
        if (Test-Path $statusPath) {
            $fleetRunning = ((Get-Content $statusPath -Raw) -match '"running"\s*:\s*true')
        }
        $breq = [System.Net.WebRequest]::Create("http://127.0.0.1:$BridgePort/status")
        $breq.Method = "GET"; $breq.Timeout = 5000; $breq.ReadWriteTimeout = 5000
        $bresp = $breq.GetResponse()
        $bbody = (New-Object System.IO.StreamReader($bresp.GetResponseStream())).ReadToEnd()
        $bresp.Close()
        try { $bj = $bbody | ConvertFrom-Json -ErrorAction Stop } catch { $bj = $null }
        if (-not $bj -or $bj.ok -ne $true) { throw "bridge /status did not return bridge JSON" }
        $bridgeBusy = ($bj.turn_running -eq $true) -or ($bj.busy -eq $true)
        $busy = $fleetRunning -or $bridgeBusy
    } catch {
        # Unreadable remains busy UNLESS the port itself proves the bridge is absent. This exact
        # distinction mattered 2026-09-28: an unrelated `python -m http.server 8765` returned
        # 404 on /status, the old catch forced busy=true forever, and a server 5.5h stale could
        # never cycle. `foreign` / `none` mean no bridge turn can exist on this port; `ours` /
        # `unknown` stay fail-closed so a damaged live bridge never loses a tool server mid-turn.
        $bv = Get-BridgePortVerdict $BridgePort
        if ($bv.Kind -eq "foreign" -or $bv.Kind -eq "none") {
            $bridgeBusy = $false
            $busy = $fleetRunning
            if ($bv.Kind -eq "foreign") {
                Write-Log ("bridge status is unavailable because :$BridgePort is held by a foreign process; " +
                           "treating the bridge as absent for stale-server safety: " + $bv.Desc)
            }
        } else {
            $busy = $true
        }
    }
    if ($busy) { $script:StaleStreak = 0; return }

    $script:StaleStreak++
    if ($script:StaleStreak -lt 2) { return }
    $script:StaleStreak = 0
    Write-Log "server is running stale code and nothing is in flight -- cycling it so the checkout's fixes are live"
    # DECLARE THE REASON BEFORE STOPPING IT. This is the specific, human-legible reason
    # Get-ServerExitRecord (see Start-Server) will show for this cycle's exit record instead of
    # the generic default Start-Server falls back to when nothing upstream has said why.
    $script:ServerPlannedEndReason = "stale code cycle"
    Write-ServerTransition $script:ServerPlannedEndReason
    Start-Server
}

function Test-PortListening {
    # Is ANYTHING accepting connections on the port? Distinguishes "has not bound yet" from
    # "bound and not answering", which Test-ServerUp alone cannot: both arrive as $false.
    try {
        $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        return [bool]$c
    } catch { return $false }
}

function Get-TunnelHostConnections {
    # The relay's "Host connections" count for $TunnelName, or $null when `devtunnel show` gave
    # no readable count (offline, CLI hiccup). $null is treated like 0 by the callers, as the
    # old boolean check always did.
    $out = & $DevTunnel show $TunnelName 2>$null | Out-String
    if ($out -match 'Host connections\s*:\s*(\d+)') { return [int]$Matches[1] }
    return $null
}

function Test-TunnelHosting {
    # Does ANYBODY host the tunnel. Not the question the main loop asks any more -- see
    # Resolve-TunnelHostingState -- but still the right one while a host we just launched is
    # registering (Start-TunnelHost, which also requires that host to be alive).
    $c = Get-TunnelHostConnections
    return ($null -ne $c -and $c -ge 1)
}

# -- WHOSE HOST IS SERVING THIS PC'S TUNNEL (2026-09-24) ------------------------------------------
# "Host connections >= 1" answers "does anybody host it", and the supervisor read that as "we
# host it". Measured 2026-09-24 08:31-08:48: a second PC signed in to the same account hosted
# THIS PC's tunnel. This PC's re-host "established" at 08:34:23 (the count was the other PC's),
# exited 27 s later, and from then on the count stayed 1, so the supervisor saw "hosting" and
# did nothing, while every request to this PC's URL went to the other PC and the cockpit's
# tunnel dot was red. Re-hosting would not have helped either: two machines re-hosting one
# tunnel knock each other off in a loop.
#
# THE QUESTION IS NOW THE COCKPIT'S: is THIS machine's server reachable through this tunnel --
# GET <tunnel>/health answering with the pid our loopback /health reports (main.py's /health
# returns {status, server_pid} to an unauthenticated tunnel caller, which is enough). And the
# answer has three shapes that want three different responses:
#   ours     the tunnel answers with our pid, or a devtunnel host of OURS is running and nothing
#            contradicts it -> fine.
#   none     nobody hosts it -> re-host after the debounce, exactly as before.
#   foreign  it is hosted, but not by a host process of this machine -> ANOTHER PC is serving
#            this PC's tunnel. Do not fight: say so once in the log and in .fleet\tunnel_host.json
#            (which doctor reads), with what to do. When the other PC lets go the count falls to 0
#            and this PC re-hosts by itself.
#   shared   our host runs, but the tunnel answered with ANOTHER pid -> the relay is splitting the
#            calls between this PC and another. Same response as foreign.
function Test-IsTunnelHostCommandLine {
    # PURE. `devtunnel host <name>` for THIS tunnel: the host verb followed by the tunnel's bare
    # id as a whole token (with or without its ".<cluster>" suffix) -- so "abc" does not claim
    # "abc-2". setup_devtunnel.ps1 carries the same rule (Test-IsTunnelHostCommandLine), and
    # scripts/test_tunnel_served_by_another_pc.py runs both on the same command lines.
    param([string]$CommandLine, [string]$Name)
    if (-not $CommandLine -or -not $Name) { return $false }
    if ($CommandLine -notmatch '(?i)devtunnel(\.exe|\.cmd)?"?\s+host\s') { return $false }
    $bare = Get-BareTunnelName $Name
    if (-not $bare) { return $false }
    return ($CommandLine -match ('(?i)\shost\s+"?' + [regex]::Escape($bare) + '(\.[a-z0-9]+)?("|\s|$)'))
}

function Test-OurTunnelHostRunning {
    # A devtunnel host for $TunnelName running ON THIS MACHINE: the one this supervisor launched,
    # or any devtunnel.exe whose command line hosts this tunnel (a host started by hand, or by
    # setup_devtunnel.ps1).
    if ($script:TunnelHostProc) {
        try {
            $script:TunnelHostProc.Refresh()
            if (-not $script:TunnelHostProc.HasExited) { return $true }
        } catch { }
    }
    $mine = @(Get-CimInstance Win32_Process -Filter "Name='devtunnel.exe'" -ErrorAction SilentlyContinue |
              Where-Object { Test-IsTunnelHostCommandLine ([string]$_.CommandLine) $TunnelName })
    return ($mine.Count -gt 0)
}

function Get-EnvTunnelUrl {
    try {
        $envp = Join-Path $Root ".env"
        if (Test-Path $envp) {
            $m = (Get-Content $envp | Where-Object { $_ -match '^\s*MCP_TUNNEL_URL\s*=' } | Select-Object -First 1)
            if ($m) { return ($m -replace '^\s*MCP_TUNNEL_URL\s*=\s*', '').Trim() }
        }
    } catch { }
    return ""
}

function Get-TunnelOrigin {
    # PURE. MCP_TUNNEL_URL points at /mcp; /health is a sibling at the origin (the cockpit and
    # doctor make the same correction).
    param([string]$TunnelUrl)
    if (-not $TunnelUrl) { return "" }
    try { return ([Uri]$TunnelUrl).GetLeftPart([UriPartial]::Authority) } catch { return "" }
}

function Get-HealthPidAt {
    # The server_pid a /health URL answers with, or $null (no answer, not a 200, a redirect to a
    # sign-in page, or not main.py's JSON). No redirects: a relay sign-in page is not the server.
    param([string]$Url, [int]$TimeoutMs = 6000)
    if (-not $Url) { return $null }
    try {
        $req = [System.Net.HttpWebRequest]::Create($Url)
        $req.Method = "GET"; $req.Timeout = $TimeoutMs; $req.ReadWriteTimeout = $TimeoutMs
        $req.AllowAutoRedirect = $false
        $req.Headers.Add("X-Tunnel-Skip-AntiPhishing-Page", "true")
        $resp = $req.GetResponse()
        $code = [int]$resp.StatusCode
        $body = $null
        try { $body = (New-Object System.IO.StreamReader($resp.GetResponseStream())).ReadToEnd() } finally { $resp.Close() }
        if ($code -ne 200) { return $null }
        return (Get-HealthServerPid $body)
    } catch {
        return $null
    }
}

function Resolve-TunnelHostingState {
    # PURE. See the block comment above for what each answer means.
    param($Connections, [bool]$OurHostRunning, $LocalPid, $TunnelPid)
    if ($LocalPid -and $TunnelPid -and ([int]$TunnelPid -eq [int]$LocalPid)) { return "ours" }
    if ($null -eq $Connections -or [int]$Connections -lt 1) { return "none" }
    if ($OurHostRunning) {
        if ($LocalPid -and $TunnelPid) { return "shared" }
        return "ours"
    }
    return "foreign"
}

# THE PROBE THROUGH THE TUNNEL IS BOUNDED. It is a remote round trip (up to 6 s), and in exactly
# the state it matters most -- another PC serving the URL -- it can time out on every tick. After
# a probe that got no answer the next one waits 1, 2, 4 ... ticks, capped at
# $script:TunnelProbeBackoffMax (40 ticks, about 10 minutes); an answer resets it. The relay's
# connection count is still read every tick, so "the other PC let go" is seen at once.
$script:TunnelProbeSkip = 0
$script:TunnelProbeBackoff = 1
$script:TunnelProbeBackoffMax = 40

function Reset-TunnelProbeBackoff {
    $script:TunnelProbeSkip = 0
    $script:TunnelProbeBackoff = 1
}

function Get-TunnelServerPidBounded {
    if ($script:TunnelProbeSkip -gt 0) { $script:TunnelProbeSkip--; return $null }
    $origin = Get-TunnelOrigin (Get-EnvTunnelUrl)
    if (-not $origin) { return $null }
    $p = Get-HealthPidAt ($origin + "/health") 6000
    if ($null -eq $p) {
        $script:TunnelProbeSkip = $script:TunnelProbeBackoff
        $script:TunnelProbeBackoff = [int][math]::Min($script:TunnelProbeBackoff * 2, $script:TunnelProbeBackoffMax)
    } else {
        Reset-TunnelProbeBackoff
    }
    return $p
}

# THE STATUS FILE doctor reads (.fleet\tunnel_host.json; .fleet is gitignored). Written only when
# the state or its wording changes -- not every tick -- atomically (temp file + move), no BOM.
# supervisor_pid lets a reader tell a live answer from one a dead supervisor left behind.
$script:TunnelStatusPath = Join-Path $Root ".fleet\tunnel_host.json"
$script:TunnelStatusKey = ""

function Get-TunnelHostingAdvice {
    # PURE. @{ Message; Action } in plain words for a state.
    param([string]$State, [string]$Name, $Connections, $LocalPid, $TunnelPid)
    switch ($State) {
        "foreign" {
            return @{
                Message = ("Another PC is serving this PC's tunnel '" + $Name + "': the relay reports " +
                           $Connections + " host connection(s) and no devtunnel host of this PC is running, " +
                           "so calls to this PC's URL go to that PC, not to this one. This supervisor will " +
                           "not fight it for the tunnel (two PCs re-hosting one tunnel knock each other off).")
                Action  = ("On the OTHER PC run quickstart.bat so it creates its own tunnel (and paste that " +
                           "PC's new URL into its own Copilot Studio connector); then on THIS PC run " +
                           "start_all.bat. This PC takes its tunnel back by itself as soon as the other PC " +
                           "stops hosting it.")
            }
        }
        "shared" {
            return @{
                Message = ("This PC hosts its tunnel '" + $Name + "', but so does another PC: the public URL " +
                           "answered from server pid " + $TunnelPid + ", while this PC's server is pid " +
                           $LocalPid + ", so the relay splits the calls between the two PCs.")
                Action  = ("On the OTHER PC run quickstart.bat so it creates its own tunnel (and paste that " +
                           "PC's new URL into its own Copilot Studio connector); then on THIS PC run " +
                           "start_all.bat.")
            }
        }
        "none" {
            return @{ Message = ("Nobody is hosting this PC's tunnel '" + $Name + "'; the supervisor re-hosts it.")
                      Action  = "" }
        }
        default {
            return @{ Message = ("This PC is serving its tunnel '" + $Name + "'."); Action = "" }
        }
    }
}

function Set-TunnelHostStatus {
    param([string]$State, $Connections, [bool]$OurHostRunning, $LocalPid, $TunnelPid)
    $advice = Get-TunnelHostingAdvice -State $State -Name $TunnelName -Connections $Connections `
                                      -LocalPid $LocalPid -TunnelPid $TunnelPid
    $key = $State + "|" + $TunnelName + "|" + $advice.Message
    if ($key -eq $script:TunnelStatusKey) { return }
    try {
        $dir = Split-Path -Parent $script:TunnelStatusPath
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
        $doc = [ordered]@{
            state                = $State
            tunnel               = $TunnelName
            since                = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK")
            host_connections     = $Connections
            this_pc_host_running = $OurHostRunning
            local_server_pid     = $LocalPid
            tunnel_server_pid    = $TunnelPid
            message              = $advice.Message
            action               = $advice.Action
            supervisor_pid       = $PID
        }
        $tmp = $script:TunnelStatusPath + ".tmp"
        [IO.File]::WriteAllText($tmp, ($doc | ConvertTo-Json -Depth 3), (New-Object System.Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $tmp -Destination $script:TunnelStatusPath -Force -ErrorAction Stop
        $script:TunnelStatusKey = $key
    } catch { }
}

$script:TunnelState = ""
$script:ForeignStreak = 0

function Invoke-TunnelHostingCheck {
    # One tick of tunnel management, once devtunnel is known to be logged in. Uses the main
    # loop's $tunnelMiss / $loggedIn (script scope).
    $conn = Get-TunnelHostConnections
    $ourHost = $false
    $localPid = $null
    $tunnelPid = $null
    if ($null -ne $conn -and $conn -ge 1) {
        $ourHost = Test-OurTunnelHostRunning
        $localPid = Get-HealthPidAt "http://127.0.0.1:$Port/health" 5000
        $tunnelPid = Get-TunnelServerPidBounded
    }
    $state = Resolve-TunnelHostingState -Connections $conn -OurHostRunning $ourHost `
                                        -LocalPid $localPid -TunnelPid $tunnelPid
    $wasContested = ($script:TunnelState -eq "foreign" -or $script:TunnelState -eq "shared")

    if ($state -eq "foreign" -or $state -eq "shared") {
        # NOT A MISS: re-hosting is exactly what must not happen here.
        $script:tunnelMiss = 0
        $script:ForeignStreak++
        if ($script:ForeignStreak -lt $FailuresBeforeAction) { return $state }
        if ($script:TunnelState -ne $state) {
            $advice = Get-TunnelHostingAdvice -State $state -Name $TunnelName -Connections $conn `
                                              -LocalPid $localPid -TunnelPid $tunnelPid
            Write-Log ("tunnel " + $state.ToUpper() + ": " + $advice.Message + " To fix: " + $advice.Action +
                       " (details: .fleet\tunnel_host.json)")
        }
        $script:TunnelState = $state
        Set-TunnelHostStatus -State $state -Connections $conn -OurHostRunning $ourHost `
                             -LocalPid $localPid -TunnelPid $tunnelPid
        return $state
    }

    $script:ForeignStreak = 0
    if ($wasContested) {
        Write-Log ("tunnel no longer served by another PC (now: " + $state + ", host connections = " + $conn + ")")
    }
    $script:TunnelState = $state
    if ($state -eq "ours") {
        $script:tunnelMiss = 0
        Set-TunnelHostStatus -State "ours" -Connections $conn -OurHostRunning $ourHost `
                             -LocalPid $localPid -TunnelPid $tunnelPid
        return $state
    }

    # none: nobody hosts it -> the debounce and re-host this loop always did.
    Set-TunnelHostStatus -State "none" -Connections $conn -OurHostRunning $false -LocalPid $null -TunnelPid $null
    Clear-DevtunnelLoginCache "tunnel host connections = 0"
    $script:tunnelMiss++
    Write-Log "tunnel host connections = 0 ($($script:tunnelMiss)/$FailuresBeforeAction)"
    if ($script:tunnelMiss -ge $FailuresBeforeAction) {
        # NEVER RE-HOST ON A CACHED ANSWER. The cache was just dropped, so this asks
        # the CLI live; only a clear "logged in" lets Start-TunnelHost touch devtunnel.
        if ($script:DevtunnelLoginAnswerWasCached -and -not (Test-DevtunnelLoggedInCached)) {
            Write-Log "devtunnel NOT logged in on the live re-check before re-hosting -> tunnel management PAUSED, not re-hosting."
            $script:loggedIn = $false
        } else {
            if (-not (Start-TunnelHost)) {
                Clear-DevtunnelLoginCache "the re-host did not establish"
            }
            Start-Sleep -Seconds 8
        }
        $script:tunnelMiss = 0
    }
    return $state
}

function Test-DevtunnelLoggedIn {
    # Gate ALL tunnel management on an authenticated devtunnel CLI. When NOT logged in,
    # `devtunnel host` cannot work AND -- critically -- the supervisor must not touch any
    # devtunnel process: a user running an interactive `devtunnel login` to fix exactly
    # this state would be reaped every cycle (the 2026-06-14 outage: login could never
    # complete because the kill loop killed it before the token was written). Returns
    # $true only on a clear logged-in signal; unknown/ambiguous -> $false (do nothing).
    $out = & $DevTunnel user show 2>&1 | Out-String
    if ($out -match 'Not logged in' -or $out -match 'Login required') { return $false }
    if ($out -match 'Logged in') { return $true }
    return $false
}

# ASK DEVTUNNEL WHO IS LOGGED IN WHEN THE ANSWER CAN HAVE CHANGED, NOT EVERY TICK.
# `devtunnel user show` measured 3.7 s per call on 2026-09-24, and it ran on every pass of the
# loop below -- the single largest part of a ~20 s tick whose nominal interval is 15 s, and so
# of how long a submitted job sat in pending before the queue pass saw it. A login does not
# change on its own between two ticks; it changes when someone logs out or a token expires,
# and both of those show up first as the tunnel host failing.
#
# ONLY A CLEAR "Logged in" IS CACHED. "Not logged in" and "cannot tell" (offline, CLI missing,
# odd output) are answered live every tick exactly as before: not-logged-in is the state in
# which a person is running `devtunnel login` by hand and the supervisor must notice the fix
# on the next tick, not ten minutes later; and a network failure is not a logout, so it must
# not be remembered as one either. Test-DevtunnelLoggedIn itself is unchanged.
#
# THE CACHE IS DROPPED AT ONCE (Clear-DevtunnelLoginCache) when the tunnel-hosting check
# fails, when a host this supervisor launched has exited, and when a re-host does not
# establish -- the three observations that can mean the login went away. And a re-host is
# never decided on a cached answer: the branch that would call Start-TunnelHost re-asks live
# first, so the "never touch devtunnel while logged out" rule holds exactly as before.
#
# LOGGED BRIEFLY: one line when a live re-check says "logged in" (at most once per expiry
# unless something invalidated it), one line the first time the cached answer is relied on
# after that. Not a line per tick.
$script:DevtunnelLoginCacheSeconds = 600
$script:DevtunnelLoginCachedUntil = [datetime]::MinValue
$script:DevtunnelLoginCacheUseLogged = $false
$script:DevtunnelLoginAnswerWasCached = $false
$script:DevtunnelLoginRecheckReason = "first check"

function Clear-DevtunnelLoginCache {
    param([string]$Reason)
    $script:DevtunnelLoginCachedUntil = [datetime]::MinValue
    $script:DevtunnelLoginRecheckReason = $Reason
}

function Test-DevtunnelLoggedInCached {
    $now = Get-Date
    if ($now -lt $script:DevtunnelLoginCachedUntil) {
        $script:DevtunnelLoginAnswerWasCached = $true
        if (-not $script:DevtunnelLoginCacheUseLogged) {
            $until = $script:DevtunnelLoginCachedUntil.ToString("HH:mm:ss")
            Write-Log ("devtunnel login: using the cached 'logged in' answer until $until " +
                       "(re-asked sooner if the tunnel host fails)")
            $script:DevtunnelLoginCacheUseLogged = $true
        }
        return $true
    }
    $script:DevtunnelLoginAnswerWasCached = $false
    $why = $script:DevtunnelLoginRecheckReason
    $script:DevtunnelLoginRecheckReason = $null
    $answer = [bool](Test-DevtunnelLoggedIn)
    if ($answer) {
        $script:DevtunnelLoginCachedUntil = $now.AddSeconds($script:DevtunnelLoginCacheSeconds)
        $script:DevtunnelLoginCacheUseLogged = $false
        if (-not $why) { $why = "cache expired" }
        Write-Log "devtunnel login re-checked ($why): logged in"
    } else {
        $script:DevtunnelLoginCachedUntil = [datetime]::MinValue
        # A live "no" after an invalidation is worth one line; the steady not-logged-in state
        # is already announced by the main loop's own transition log, so repeats stay silent.
        if ($why) { Write-Log "devtunnel login re-checked ($why): not logged in or cannot tell" }
    }
    return $answer
}

# TRACKS THE PROCESS THIS SUPERVISOR ITSELF LAUNCHED (see -PassThru on Start-Process inside
# Start-Server, below) and, when this supervisor is about to end it on purpose, WHY. Both start
# out $null: before this supervisor's own Start-Server has ever run, there is no process to
# read and no plan to end one, and Get-ServerExitRecord says so explicitly instead of guessing.
$script:ServerProc = $null
$script:ServerPlannedEndReason = $null

function Get-ServerExitRecord {
    # WHY THIS FUNCTION EXISTS. A process that dies silently leaves no output BY DEFINITION --
    # preserving stdout/stderr (the history-log dance in Start-Server, below) can explain a
    # crash that printed a traceback, but it can never explain one that printed nothing, because
    # there is nothing in either stream to preserve. Measured 2026-09-16: the server exited six
    # times in two days and every preserved stderr block held only the uvicorn startup banner --
    # no traceback, no "Shutting down", and Windows Error Reporting logged no fault for it
    # either. What DOES survive a silent death is the process's own exit code and the moment it
    # happened -- and until this function existed, Start-Server discarded both ON PURPOSE: it
    # called Start-Process without -PassThru, so the .NET Process object (and with it .ExitCode
    # / .HasExited / .ExitTime) was thrown away the instant the call returned. This is the read
    # side of fixing that: given the process object this supervisor stored when it launched the
    # server, report whether it has exited, its exit code in both decimal and the unsigned hex
    # form crash codes are conventionally read in (0xC0000005 access violation, 0xC0000409 stack
    # buffer overrun, ...), and whether the supervisor itself ended it on purpose or it ended on
    # its own.
    #
    # PLANNED VS UNPLANNED MUST COME FROM THE CALLER, NOT BE GUESSED HERE. By the time anyone
    # calls this, the process may already have been Stop-Process'd by Start-Server's own kill-
    # by-port cleanup below -- so "has it exited" cannot by itself say whether WE ended it just
    # now or it was already dead before we got there. Start-Server captures that distinction the
    # only place it can be captured honestly: at its own top, before it does anything, by
    # checking whether the previous process was still alive at that instant. -PlannedReason is
    # that judgement, already made; this function only formats it.
    #
    # NOT JUST FOR THE SERVER. Invoke-FleetAutoResume / Invoke-ReviewAutoResume (below) launch
    # relay.fleet_runner / bench.review_run the same way Start-Server launches main.py, and
    # threw away the same process object -- see Invoke-AutoResumeRunnerCheck's header for why
    # that one is worth reading back too. LifetimeSeconds exists for that caller: it needs
    # sub-minute precision to tell "died before argparse" apart from a normal multi-hour run,
    # which the human-facing Detail string's "Nh Nm" rounding cannot give it. Adding the field
    # changes nothing about ExitCode/ExitCodeHex/Planned/Summary/Detail for the server's own
    # callers -- it is read from the same $span this function was already computing.
    param(
        [System.Diagnostics.Process]$Process,
        $LaunchTime,
        [string]$PlannedReason
    )
    if (-not $Process) {
        return [PSCustomObject]@{
            ServerPid       = $null
            LaunchTime      = $LaunchTime
            HasExited       = $null
            ExitCode        = $null
            ExitCodeHex     = $null
            ExitTime        = $null
            LifetimeSeconds = $null
            Planned         = $false
            PlannedReason   = $null
            Summary         = "no record: not launched by this supervisor"
            Detail          = "no record: not launched by this supervisor"
        }
    }
    $procPid = $Process.Id
    try { $Process.Refresh() } catch { }
    $hasExited = $true
    try { $hasExited = $Process.HasExited } catch { $hasExited = $true }
    if (-not $hasExited) {
        return [PSCustomObject]@{
            ServerPid       = $procPid
            LaunchTime      = $LaunchTime
            HasExited       = $false
            ExitCode        = $null
            ExitCodeHex     = $null
            ExitTime        = $null
            LifetimeSeconds = $null
            Planned         = -not [string]::IsNullOrEmpty($PlannedReason)
            PlannedReason   = $PlannedReason
            Summary         = "still running, replaced"
            Detail          = "pid=$procPid still running, replaced"
        }
    }
    $exitCode = $null
    $exitTime = $null
    try { $exitCode = $Process.ExitCode } catch { }
    try { $exitTime = $Process.ExitTime } catch { }
    $hex = $null
    if ($null -ne $exitCode) {
        # NEVER [uint32]$exitCode DIRECTLY. That is a CHECKED conversion in PowerShell and
        # throws OverflowException for any negative value -- i.e. for every crash code, which is
        # exactly the case this function exists for. BitConverter reinterprets the same 4 bytes
        # UNCHECKED instead, which is what "read a signed Int32 as its unsigned hex form" means.
        $bytes = [BitConverter]::GetBytes([int32]$exitCode)
        $hex = "0x{0:X8}" -f ([BitConverter]::ToUInt32($bytes, 0))
    }
    $ageStr = $null
    $lifetimeSeconds = $null
    if ($LaunchTime -and $exitTime) {
        $span = $exitTime - $LaunchTime
        if ($span.TotalSeconds -ge 0) {
            $ageStr = "{0}h{1}m" -f [int]$span.TotalHours, $span.Minutes
            $lifetimeSeconds = $span.TotalSeconds
        }
    }
    $planned = -not [string]::IsNullOrEmpty($PlannedReason)
    $summary = if ($planned) { "planned: $PlannedReason" } else { "unplanned" }
    $codePart = "code=$hex ($exitCode)"
    $agePart = if ($ageStr) { " after $ageStr" } else { "" }
    [PSCustomObject]@{
        ServerPid       = $procPid
        LaunchTime      = $LaunchTime
        HasExited       = $true
        ExitCode        = $exitCode
        ExitCodeHex     = $hex
        ExitTime        = $exitTime
        LifetimeSeconds = $lifetimeSeconds
        Planned         = $planned
        PlannedReason   = $PlannedReason
        Summary         = $summary
        Detail          = "pid=$procPid exited $codePart$agePart -- $summary"
    }
}

function Start-Server {
    # CAPTURE THE PREVIOUS LAUNCH'S STATE BEFORE TOUCHING ANYTHING BELOW. Everything past this
    # point can end the previous process -- deliberately, via the kill-by-port/kill-stale-
    # instances cleanup a few lines down, or incidentally if it had already died on its own --
    # and Stop-Process -Force makes a previous process register as exited either way. Read
    # "was it alive right now" AFTER that cleanup runs and the two are indistinguishable from
    # state alone; only a snapshot taken NOW, before anything here acts, can tell "we ended it"
    # apart from "it had already ended".
    #
    # (Two read-only steps run first; neither stops anything. The interpreter is re-decided
    # (D2, Update-PythonInterpreter), and the port's holders are identified (D13): when every
    # holder is something other than this checkout's server, nothing is stopped and nothing is
    # launched -- a new main.py could not bind anyway -- and the log says who holds it and
    # what to do. See the block above Get-PortListenerPids.)
    Update-PythonInterpreter "the server launch"
    $listeners = Get-PortListenerPids
    $oursOnPort = @()
    $foreignOnPort = @()
    foreach ($holderPid in $listeners.Pids) {
        $v = Get-ProcessVerdict $holderPid
        if ($v.Ours -eq $true) { $oursOnPort += $holderPid } else { $foreignOnPort += $v }
    }
    if ($foreignOnPort.Count -gt 0 -and $oursOnPort.Count -eq 0) {
        Write-ForeignPortHolderLog $foreignOnPort
        return
    }
    $prevProc = $script:ServerProc
    $prevLaunchAt = $script:LastLaunchAt
    $prevWasAlive = $false
    if ($prevProc) {
        try { $prevProc.Refresh() } catch { }
        try { $prevWasAlive = -not $prevProc.HasExited } catch { $prevWasAlive = $false }
    }
    if ($prevWasAlive -and -not $script:ServerPlannedEndReason) {
        # DEFAULT PLANNED REASON. Nobody upstream (e.g. Invoke-StaleServerCycle, which sets its
        # own reason before calling us) declared why this relaunch is happening, but we are
        # about to forcibly stop a server that was alive a moment ago -- that stop is still OUR
        # doing, not a silent death, so it must still read as planned rather than falling
        # through to "unplanned" for want of a label. A process that was already dead before we
        # got here is left alone: $prevWasAlive is $false for it, and no reason is set.
        $script:ServerPlannedEndReason = "relaunch: clearing running instance before starting a new one"
    }

    # Kill the stale instance FIRST. A wedged main.py (dead event loop, CLOSE_WAIT
    # pile-up -- seen twice on 2026-06-12/13) keeps $Port LISTENING, so a new instance
    # can't bind and the supervisor restart-loops forever while the outage persists.
    # Scope: the port's holders that are THIS CHECKOUT'S main.py (D13 -- it used to be every
    # holder), any main.py launched from this checkout, and the server this supervisor launched
    # (which on a PATH python names no checkout in its command line). A foreign holder that
    # shares the port with ours (IPv4/IPv6 on one port) is left alone.
    foreach ($holderPid in $oursOnPort) { Stop-Process -Id $holderPid -Force -ErrorAction SilentlyContinue }
    foreach ($f in $foreignOnPort) { Write-Log "leaving $($f.Desc) alone on :$Port -- it is not this checkout's server" }
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        Test-IsThisCheckoutServerCommandLine ([string]$_.CommandLine) $Root
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    if ($prevWasAlive) { try { Stop-Process -Id $prevProc.Id -Force -ErrorAction SilentlyContinue } catch { } }
    Start-Sleep -Seconds 2
    $script:LastLaunchAt = Get-Date
    Push-Location $Root
    # ITS STARTUP ERROR WAS GOING NOWHERE. Hidden window, no redirection: when main.py dies on
    # startup -- a missing dependency, a port already bound, an unreadable .env -- the reason is
    # discarded and the only trace is the line below saying it was "(re)started". The doctor then
    # says "run start_all.bat", which is what this supervisor is already doing on a loop.
    $logDir = Join-Path $Root ".setup\logs"
    try { if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force $logDir | Out-Null } } catch { }
    $srvOut = Join-Path $logDir "server.log"
    $srvErr = Join-Path $logDir "server.err.log"

    # THE PREVIOUS LAUNCH'S EXIT RECORD -- see Get-ServerExitRecord's own header for the full
    # reasoning (in short: output can never explain a silent death, but the exit code can, and a
    # planned cycle must not be allowed to read like a crash). $prevWasAlive was captured at the
    # very top of this function, before the kill-by-port/kill-stale-instances cleanup above could
    # have changed it, so a $null reason here truthfully means "it was already gone before we
    # touched it" rather than "we forgot to say why".
    $exitRecord = Get-ServerExitRecord -Process $prevProc -LaunchTime $prevLaunchAt `
                  -PlannedReason $(if ($prevWasAlive) { $script:ServerPlannedEndReason } else { $null })
    $script:ServerPlannedEndReason = $null   # consumed -- the next relaunch starts undeclared again
    if ($exitRecord.Detail -ne "no record: not launched by this supervisor") {
        Write-Log "previous server $($exitRecord.Detail)"
    }

    # PRESERVE THE PREVIOUS LAUNCH BEFORE TRUNCATING IT. -RedirectStandardError truncates, and
    # this function runs about once a minute while the server is failing, so the one file that
    # explains the crash is erased by the next attempt -- roughly sixty times an hour. Anyone
    # reading it afterwards, as the doctor told them to, sees only the newest launch and quite
    # possibly nothing at all. Carry it into a history file first, newest last, capped so an
    # unattended machine cannot fill its disk with the same stack trace.
    # BOTH STREAMS, NOT JUST stderr. Measured 2026-09-16: the server exited six times in
    # two days and every preserved stderr block holds nothing but the uvicorn startup
    # banner -- no traceback, no "Shutting down", and Windows Error Reporting logged no
    # fault for it either. So the process is vanishing silently, and the one stream that
    # might say why (uvicorn's access log, and anything the server prints) was being
    # truncated unread on every relaunch. Preserving one stream and discarding the other
    # left the failure permanently undiagnosable: the reason this comment's own argument
    # was written for stderr applies word for word to stdout.
    foreach ($pair in @(@($srvErr, "server.err.history.log"), @($srvOut, "server.out.history.log"))) {
        $live = $pair[0]
        $hist = Join-Path $logDir $pair[1]
        try {
            if ((Test-Path $live) -and ((Get-Item $live).Length -gt 0)) {
                $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
                # SAME FACTS AS THE LOG LINE ABOVE, IN THE FILE THAT SURVIVES THE LOG ROTATING.
                # Without this, a preserved block that holds nothing but the uvicorn banner (the
                # silent-death case this whole mechanism exists for) looks IDENTICAL to a block
                # preserved because the supervisor deliberately cycled a healthy server -- the
                # header used to say only when the launch ended, never why.
                Add-Content -Path $hist -Value ("=== launch ending " + $stamp + " -- " + $exitRecord.Detail + " ===") -Encoding UTF8
                Get-Content $live -ErrorAction Stop | Add-Content -Path $hist -Encoding UTF8
                # Trim from the front: the oldest crash is the least useful and the newest
                # must never be the one that gets dropped.
                if ((Get-Item $hist).Length -gt 262144) {
                    $keep = Get-Content $hist -Tail 400 -ErrorAction Stop
                    Set-Content -Path $hist -Value $keep -Encoding UTF8
                }
            }
        } catch { }
    }

    # -PASSTHRU ON BOTH CALLS. This is the write side of the fix Get-ServerExitRecord reads from
    # (see its header above): without -PassThru, Start-Process discards the .NET Process object
    # the instant the call returns, and with it .ExitCode / .ExitTime / .HasExited -- the only
    # things that survive a silent death. Both branches launch the server that becomes THIS
    # cycle's $script:ServerProc, so both must capture it or half of all launches (whichever one
    # hit the redirection-failed fallback) would go back to being unrecorded.
    $newProc = $null
    try {
        $newProc = Start-Process -FilePath $Py -ArgumentList "main.py" -WorkingDirectory $Root `
                      -WindowStyle Hidden -RedirectStandardOutput $srvOut -RedirectStandardError $srvErr -PassThru
    } catch {
        # Redirection can fail if a previous instance still holds the file. Starting the server
        # matters more than capturing it, so fall back rather than leave the stack down.
        Write-Log "server output could not be redirected ($($_.Exception.Message)); starting without it"
        $newProc = Start-Process -FilePath $Py -ArgumentList "main.py" -WorkingDirectory $Root -WindowStyle Hidden -PassThru
    }
    $script:ServerProc = $newProc
    Pop-Location
    # RECORD THE SHA THIS SERVER STARTED ON, so doctor can later tell a running server
    # apart from a checkout that has since moved past it (a `git pull` lands new
    # relay/tools/main.py on disk, but THIS long-lived process keeps executing the code
    # it imported here). The marker is a sidecar file next to the other .setup state,
    # written best-effort: failing to record it must never stop the server coming up.
    # doctor reads it via scripts\stale_server_check.py; see that module's header.
    try {
        $head = (& git -C $Root rev-parse HEAD 2>$null)
        if ($LASTEXITCODE -eq 0 -and $head) {
            $markerDir = Join-Path $Root ".setup"
            if (-not (Test-Path $markerDir)) { New-Item -ItemType Directory -Force $markerDir | Out-Null }
            Set-Content -Path (Join-Path $markerDir "server_started_head.txt") `
                        -Value $head.Trim() -Encoding ASCII -ErrorAction Stop
        }
    } catch { }
    # SAY WHAT WAS DONE, NOT WHAT WAS ACHIEVED. This used to read as though the server was up.
    # Whether it stayed up is decided by the health probe on the next pass, not here.
    Write-Log "MCP server process launched (stale instances cleared); output -> $srvErr"
}

function Start-TunnelHost {
    # Stop only OUR stale host process(es) -- those whose command line is
    # `devtunnel host <TunnelName>`. NEVER `Get-Process devtunnel | Stop-Process`: that
    # reaps a user's interactive `devtunnel login` (the very command that authenticates
    # this CLI) and any unrelated tunnel the user hosts. Killing a login mid-flow leaves
    # an empty 0-byte token + an orphaned browser dialog -- the 2026-06-14 onboarding
    # outage (see docs/STARTUP_devtunnel_login.md).
    Get-CimInstance Win32_Process -Filter "Name='devtunnel.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match '\bhost\b' -and $_.CommandLine -match [regex]::Escape($TunnelName) } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    # -PASSTHRU SO THE MAIN LOOP CAN SEE THIS HOST EXIT. A host that exits is one of the three
    # events that drop the cached devtunnel login answer (see Test-DevtunnelLoggedInCached).
    $script:TunnelHostProc = $null
    Reset-TunnelProbeBackoff
    # ITS OUTPUT IS KEPT. On 2026-09-24 a host this supervisor launched exited 27 s after it
    # "established", and the only record was that it had exited: the window was hidden and
    # nothing was redirected, so whatever devtunnel said about why was gone. The host's output
    # goes to .setup\logs\devtunnel-host.*.log (gitignored) and the main loop quotes its last
    # lines when the host exits.
    $logDir = Join-Path $Root ".setup\logs"
    $script:TunnelHostOut = Join-Path $logDir "devtunnel-host.out.log"
    $script:TunnelHostErr = Join-Path $logDir "devtunnel-host.err.log"
    try {
        if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force $logDir | Out-Null }
        $script:TunnelHostProc = Start-Process -FilePath $DevTunnel -ArgumentList "host $TunnelName" -WindowStyle Hidden `
            -RedirectStandardOutput $script:TunnelHostOut -RedirectStandardError $script:TunnelHostErr -PassThru
    } catch {
        Write-Log "devtunnel host output could not be redirected ($($_.Exception.Message)); starting without it"
        $script:TunnelHostOut = $null
        $script:TunnelHostErr = $null
        $script:TunnelHostProc = Start-Process -FilePath $DevTunnel -ArgumentList "host $TunnelName" -WindowStyle Hidden -PassThru
    }
    Write-Log "devtunnel host starting for $TunnelName (bin=$DevTunnel) ..."
    # A freshly-started host can take 15-30s to register with the relay. Block until it
    # actually shows >=1 connection (up to ~50s) so the monitor loop never kills a host
    # that is still in the middle of connecting (which would cause a restart churn loop).
    # RETURNS WHETHER IT ESTABLISHED: a re-host that does not is the third invalidation event.
    # AND THE CONNECTION MUST BE OURS: a count of 1 while another PC hosts this tunnel said
    # "established" on 2026-09-24 for a host that exited seconds later. So the host we launched
    # must still be alive when the count is read, and if it exits first that is the answer.
    for ($i = 0; $i -lt 25; $i++) {
        Start-Sleep -Seconds 2
        $exited = $false
        try { $script:TunnelHostProc.Refresh(); $exited = $script:TunnelHostProc.HasExited } catch { $exited = $true }
        if ($exited) {
            Write-Log ("devtunnel host exited while starting" + (Get-TunnelHostExitDetail) + " (will retry next cycle)")
            return $false
        }
        if (Test-TunnelHosting) {
            Write-Log "devtunnel host established (after ~$([int](($i + 1) * 2))s)"
            return $true
        }
    }
    Write-Log "devtunnel host did not establish within ~50s (will retry next cycle)"
    return $false
}
$script:TunnelHostProc = $null
$script:TunnelHostOut = $null
$script:TunnelHostErr = $null

function Get-TunnelHostExitDetail {
    # " (code N; it said: ...)" for the host this supervisor launched, from its exit code and the
    # last lines it wrote. Empty when nothing is known.
    $parts = @()
    try { if ($script:TunnelHostProc) { $parts += ("code " + $script:TunnelHostProc.ExitCode) } } catch { }
    $said = @()
    foreach ($f in @($script:TunnelHostErr, $script:TunnelHostOut)) {
        if (-not $f) { continue }
        try {
            if (Test-Path -LiteralPath $f) {
                $said += @(Get-Content -LiteralPath $f -Tail 3 -ErrorAction Stop | Where-Object { $_.Trim() } |
                           ForEach-Object { $_.Trim() })
            }
        } catch { }
    }
    if ($said.Count -gt 0) { $parts += ("it said: " + (($said | Select-Object -Last 3) -join " | ")) }
    if ($parts.Count -eq 0) { return "" }
    return (" (" + ($parts -join "; ") + ")")
}

# ── Fleet coordinator auto-resume ───────────────────────────────────────────────
# A fleet run (python -m relay.fleet_runner) killed by an unplanned reboot leaves
# .fleet\fleet_run_active.json behind: fleet_runner.py writes it once at run start and
# removes it on CLEAN completion or an explicit user stop (Ctrl+C / the graceful `stop`
# command via commands.json -- both reach the same normal end-of-main() path that clears
# it; see _write_active_marker / _clear_active_marker / should_auto_resume in
# relay/fleet_runner.py). So the marker surviving with a DEAD pid means the run was
# genuinely INTERRUPTED, not finished and not deliberately stopped -- there is no separate
# persistent "user stopped" signal to check here because an explicit stop already clears
# the marker itself before this code ever runs.
#
# Checked ONCE at startup (not on the health-check loop below): a fresh boot is the only
# time an interrupted run needs discovering. The existing single-instance Mutex above
# already makes this idempotent -- a second concurrent supervisor exits before reaching
# this point, so it can never double-relaunch.
#
# Opt-out: set MCP_FLEET_AUTORESUME=0 (or false/no/off) in the environment. Default ON.
$FleetDir = Join-Path $Root ".fleet"
$FleetMarkerPath = Join-Path $Root ".fleet\fleet_run_active.json"
$ReviewMarkerPath = Join-Path $Root ".fleet\review_run_active.json"
$LocalLoopMarkerDir = Join-Path $Root ".fleet\local_loop_active"
$LocalLoopCampaignPath = Join-Path $FleetDir "local_loop_campaign.json"
$script:LastReviewResumeKey = ""
$script:LastReviewResumeAttempt = [datetime]::MinValue

# TRACKS AUTO-RESUME RUNNERS THIS SUPERVISOR ITSELF LAUNCHED (relay.fleet_runner / bench.review_run / relay.local_loop_controller below), so a LATER tick can read back their exit code instead of the
# process vanishing the moment Start-Process returns -- the exact defect Get-ServerExitRecord
# exists to fix for the MCP server, here applied one level up.
#
# WHY THIS MATTERS MORE HERE, NOT LESS. docs/agent_contract.md says a runner that "dies before
# argparse (a bad path, the wrong interpreter, a failed import)" leaves NO record at all,
# because from inside that process nothing has run yet that could write one -- there is no log
# file, no marker, nothing. The supervisor is the one process that can ALWAYS see it, because
# it is the parent: Start-Process's return value already carries the exit code, and until now
# it was thrown away here exactly like it was for the server. A LIST, NOT A SINGLE SLOT: the
# fleet marker is only checked once at startup, but review auto-resume runs every tick and can
# relaunch more than once in a long session, so more than one runner can be pending a report
# at the same time.
$script:AutoResumeRunners = New-Object System.Collections.Generic.List[object]

function Register-AutoResumeRunner {
    # Call this right after a Start-Process -PassThru for an auto-resume relaunch, so
    # Invoke-AutoResumeRunnerCheck (below) has something to read back on a later tick.
    # $Proc may be $null (Start-Process failing before returning a process, or the call sat
    # inside a try/catch that never reached it) -- silently does nothing then, since there is
    # nothing to check back on; the existing Write-Log in the catch block already covers that
    # failure on its own.
    param(
        [System.Diagnostics.Process]$Proc,
        [datetime]$LaunchTime,
        [string]$Kind,
        [string]$CommandLine
    )
    if (-not $Proc) { return }
    $script:AutoResumeRunners.Add([PSCustomObject]@{
        Proc        = $Proc
        LaunchTime  = $LaunchTime
        Kind        = $Kind
        CommandLine = $CommandLine
    })
}

# HOW QUICKLY A DEATH COUNTS AS "DID NOT GET FAR ENOUGH TO DO ANYTHING" rather than a normal
# end that happens to be short. Not an arbitrary guess: docs/agent_contract.md's own examples
# (a bad path, the wrong interpreter, a failed import) fail at argparse or at the first
# import, both well under a second; a coordinator that got INTO its run loop and died later is
# a different failure with its own diagnostics already (fleet_reaper.py's stale-run reap,
# review's own retry_after backoff) and does not need this flag on top.
$script:AutoResumeQuickDeathSeconds = 30

function Invoke-AutoResumeRunnerCheck {
    # Called once per main-loop tick (see the while loop below). Reports each tracked runner
    # EXACTLY ONCE, on the first tick after it has exited -- entries still running are simply
    # left in the list for the next tick, never reported (Get-ServerExitRecord's own "still
    # running, replaced" case is deliberately not logged here: a runner just relaunched is
    # expected to still be running on the very next 15-second tick, and logging that every
    # cycle would be noise, not signal).
    if ($script:AutoResumeRunners.Count -eq 0) { return }
    $stillPending = New-Object System.Collections.Generic.List[object]
    foreach ($entry in $script:AutoResumeRunners) {
        $rec = Get-ServerExitRecord -Process $entry.Proc -LaunchTime $entry.LaunchTime -PlannedReason $null
        if (-not $rec.HasExited) {
            $stillPending.Add($entry)
            continue
        }
        $quick = ($null -ne $rec.LifetimeSeconds) -and
                 ($rec.LifetimeSeconds -lt $script:AutoResumeQuickDeathSeconds) -and
                 ($null -ne $rec.ExitCode) -and ($rec.ExitCode -ne 0)
        if ($quick) {
            $secs = [int][math]::Round($rec.LifetimeSeconds)
            # PROMINENT AND ACTIONABLE. This is the exact scenario the whole function exists
            # for: the runner died before it could log anything about itself, so the supervisor
            # says so explicitly instead of leaving a bare exit code, and prints the COMMAND
            # LINE so a human can run the identical thing by hand and see the real error on
            # their own console (argparse and import errors print to stderr, which this
            # process's own hidden, unredirected Start-Process never captured).
            Write-Log ("$($entry.Kind) auto-resume runner died after ${secs}s, code=$($rec.ExitCodeHex) " +
                       "($($rec.ExitCode)) -- it did not get far enough to log anything itself; " +
                       "run the same command by hand to see why: $($entry.CommandLine)")
        } else {
            # A NORMAL END -- exit 0, or a nonzero code after running long enough that it is
            # not the "dead before argparse" case. Logged briefly: one line of the same facts,
            # without the "unplanned" language Get-ServerExitRecord's own Detail carries for
            # the server (the supervisor never deliberately stops an auto-resume runner, so
            # that distinction does not apply here and would only read as alarming noise on a
            # routine finish).
            $ageBit = if ($rec.LifetimeSeconds -ne $null) {
                if ($rec.LifetimeSeconds -lt 120) { "$([int][math]::Round($rec.LifetimeSeconds))s" }
                else { "{0}h{1}m" -f [int]([timespan]::FromSeconds($rec.LifetimeSeconds)).TotalHours, ([timespan]::FromSeconds($rec.LifetimeSeconds)).Minutes }
            } else { "unknown duration" }
            Write-Log ("$($entry.Kind) auto-resume runner (pid=$($rec.ServerPid)) exited code=$($rec.ExitCodeHex) " +
                       "($($rec.ExitCode)) after $ageBit")
        }
    }
    $script:AutoResumeRunners = $stillPending
}

function Test-FleetAutoResumeEnabled {
    # THE PRIMARY SWITCH IS THE OPERATOR'S SETTING `fleet_auto_resume` (cockpit gear popup, section
    # Recovery), read through the product's own reader (relay.fleet_resume.auto_resume_setting) so
    # the file and its default are not parsed twice. Precedence: MCP_FLEET_AUTORESUME when set
    # (the old override, still honoured), then -Force (the old -FleetCycleResumeLive switch), then
    # the setting. A reader that cannot run reads as the registry default, ON.
    # Called only once a resume candidate exists, so an ordinary tick spawns no python for it.
    param([switch]$Force)
    $v = $env:MCP_FLEET_AUTORESUME
    if (-not [string]::IsNullOrWhiteSpace($v)) {
        return -not ($v -in @("0", "false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF"))
    }
    if ($Force) { return $true }
    $setting = ""
    try {
        $setting = ([string](& $Py -c "import sys; sys.path.insert(0, r'$Root'); from relay import fleet_resume; print(fleet_resume.auto_resume_setting())" 2>$null)).Trim()
    } catch { $setting = "" }
    return ($setting -ne "off")
}

function Get-FleetActiveMarker {
    # Tolerant read mirroring relay.fleet_runner._read_active_marker(): missing or
    # corrupt JSON -> $null, never throws.
    if (-not (Test-Path $FleetMarkerPath)) { return $null }
    try {
        $raw = Get-Content -Path $FleetMarkerPath -Raw -ErrorAction Stop
        return $raw | ConvertFrom-Json -ErrorAction Stop
    } catch {
        return $null
    }
}

function Test-PidAlive {
    param([int]$ProcId)
    if (-not $ProcId -or $ProcId -le 0) { return $false }
    return [bool](Get-Process -Id $ProcId -ErrorAction SilentlyContinue)
}

function Test-FleetShouldAutoResume {
    # PURE decision, mirrors relay.fleet_runner.should_auto_resume(): marker present AND
    # its recorded pid is DEAD -> resume. Marker absent, or its pid is still alive
    # (already running -- never double-launch) -> do nothing. Kept side-effect free so it
    # can be exercised with a fake marker + a known-dead pid (see docs/validation notes).
    #
    # With -Gate it ALSO applies the loop guard, mirroring relay.fleet_resume.resume_gate()
    # (Get-FleetResumeGate below): stop requested, coordinator live, max 3 automatic resumes,
    # backoff 5 min * 2^count, disk floor (READ from settings, never chosen here), and the same
    # crash with no more free space than last time. Without -Gate the old behaviour is unchanged.
    param($Marker, [switch]$Gate, $Record = $null, $FreeBytes = $null, $FloorGb = $null,
          [string]$Signature = "", [double]$Now = 0, [switch]$CoordinatorLive, [switch]$Enospc)
    if ($null -eq $Marker) { return $false }
    $procId = 0
    try { $procId = [int]$Marker.pid } catch { return $false }
    if ($procId -le 0) { return $false }
    if (Test-PidAlive -ProcId $procId) { return $false }
    if (-not $Gate) { return $true }
    $reason = Get-FleetResumeGate -Record $Record -Now $Now -FreeBytes $FreeBytes -FloorGb $FloorGb `
        -Signature $Signature -CoordinatorLive:$CoordinatorLive -Enospc:$Enospc
    return ($reason -eq "ok")
}

function Get-FleetResumeGate {
    # PURE. MIRRORS relay.fleet_resume.resume_gate(): returns "ok" or the refusal reason, first
    # refusal wins, same order and same reason strings. scripts/test_supervisor_fleet_resume_guard.py
    # feeds one table of cases to both and fails if they differ. MAX 3 / 300 s are the design's
    # proposed loop-guard numbers (relay.fleet_resume.MAX_AUTO_RESUMES / BACKOFF_BASE_S).
    param($Record = $null, [double]$Now = 0, $FreeBytes = $null, $FloorGb = $null,
          [string]$Signature = "", [switch]$CoordinatorLive, [switch]$Enospc)
    if ($Now -le 0) { $Now = [double][DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }
    $intr = $null
    if ($Record) { $intr = $Record.interrupted }
    if ($Record -and $Record.stop_requested -eq $true) { return "stop_requested" }
    if ($intr -and $intr.stop_requested -eq $true) { return "stop_requested" }
    if ($CoordinatorLive) { return "coordinator_live" }
    $state = "pending"
    if ($Record -and $Record.state) { $state = [string]$Record.state }
    if ($state -ne "pending") { return ("state_" + $state) }
    $res = $null
    if ($Record) { $res = $Record.resume }
    $count = 0
    if ($res -and $null -ne $res.count) { $count = [int]$res.count }
    if ($count -ge 3) { return "max_resumes" }
    $lastTs = $null
    if ($res -and $null -ne $res.last_ts) { $lastTs = [double]$res.last_ts }
    if ($count -gt 0 -and $null -ne $lastTs) {
        if ($Now -lt ($lastTs + (300.0 * [math]::Pow(2, $count)))) { return "backoff" }
    }
    $free = $null
    if ($null -ne $FreeBytes) { $free = [double]$FreeBytes }
    $floor = $null
    if ($null -ne $FloorGb) { $floor = [double]$FloorGb }
    if ($null -ne $floor -and $floor -gt 0 -and $null -ne $free -and $free -lt ($floor * 1073741824.0)) {
        return "below_floor"
    }
    $prev = $null
    if ($res -and $null -ne $res.last_free_bytes) { $prev = [double]$res.last_free_bytes }
    elseif ($intr -and $null -ne $intr.free_bytes_at_detection) { $prev = [double]$intr.free_bytes_at_detection }
    elseif ($intr -and $null -ne $intr.free_bytes_at_death) { $prev = [double]$intr.free_bytes_at_death }
    $noGain = ($null -eq $free) -or ($null -eq $prev) -or ($free -le $prev)
    $lastSig = ""
    if ($res -and $res.last_signature) { $lastSig = [string]$res.last_signature }
    if ($Signature -and ($Signature -eq $lastSig) -and $noGain) { return "same_crash_no_more_space" }
    if ($Enospc -and $noGain) { return "disk_full_no_more_space" }
    return "ok"
}

function Get-FleetPendingSnapshot {
    # The newest .fleet\interrupted\*.json whose state is "pending" (relay.fleet_reaper writes
    # it before it removes the live marker), as @{ Path; Data }, or $null. Mirrors
    # relay.fleet_reaper.read_interrupted_snapshot(). Never throws.
    try {
        $dir = Join-Path $FleetDir "interrupted"
        if (-not (Test-Path $dir)) { return $null }
        $best = $null
        foreach ($f in (Get-ChildItem -Path $dir -Filter "*.json" -File -ErrorAction SilentlyContinue)) {
            try { $d = (Get-Content -Path $f.FullName -Raw -ErrorAction Stop) | ConvertFrom-Json -ErrorAction Stop } catch { continue }
            if (-not $d -or $d.state -ne "pending") { continue }
            $ts = 0.0
            try { $ts = [double]$d.written_ts } catch { $ts = 0.0 }
            if ($null -eq $best -or $ts -gt $best.Ts) { $best = @{ Path = $f.FullName; Data = $d; Ts = $ts } }
        }
        return $best
    } catch { return $null }
}

# A COORDINATOR OF THIS CHECKOUT THAT IS ALREADY RUNNING. A MIRROR, NOT A SHARED COPY, of
# start_all.ps1's function of the same name (added in 1f4588a): sharing it means moving it into
# a dot-sourced helper and changing start_all.ps1 to load it, and start_all.ps1 is not this
# change's file. The body is kept textually identical ($Root and $root are one variable to
# PowerShell), and scripts/test_supervisor_fleet_resume_guard.py fails if the two ever differ.
function Get-ThisCheckoutFleetCoordinatorPids {
    try {
        return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                 Where-Object { $_.CommandLine -and ($_.CommandLine -match 'relay[\\/.]fleet_runner') -and
                                ($_.CommandLine -like "*$root*") } |
                 ForEach-Object { $_.ProcessId })
    } catch { return @() }
}

# THE RUN THIS SUPERVISOR JUST RESUMED, until it has written its own marker. See
# Get-FleetReapHoldReason.
$script:ResumedFleet = $null

# THE PER-TICK RESUME CHECK RUNS EVERY CYCLE and, in dry-run or while refused, would repeat the
# same lines every cycle. Keys already logged in this supervisor's life are not logged again.
$script:FleetCycleNoted = @{}

function Write-FleetResumeLog {
    param([string]$Msg, [string]$Key, [switch]$Once)
    if ($Once) {
        if ($script:FleetCycleNoted.ContainsKey($Key)) { return }
        $script:FleetCycleNoted[$Key] = $true
    }
    Write-Log $Msg
}

function Invoke-FleetAutoResume {
    # Returns $true iff it (would have) relaunched the coordinator; $false otherwise.
    # -DryRun logs the would-be relaunch command without starting a process.
    # The resume SOURCE is the live marker, else the marker copy in the newest pending
    # .fleet\interrupted\*.json (the reaper removes the live marker once it has marked the
    # run interrupted). A snapshot source is also put through the loop guard
    # (Get-FleetResumeGate). -FromCycle marks the per-tick call, which is quieter when there is
    # nothing to do.
    param([switch]$DryRun, [switch]$FromCycle, [switch]$ForceEnabled)
    $marker = Get-FleetActiveMarker
    $snap = $null
    if ($null -eq $marker) {
        $snap = Get-FleetPendingSnapshot
        if ($snap -and $snap.Data.marker) { $marker = $snap.Data.marker }
    }
    if (-not (Test-FleetShouldAutoResume $marker)) { return $false }
    # THE SWITCH IS ASKED ONLY NOW, with a candidate in hand (see Test-FleetAutoResumeEnabled).
    if (-not (Test-FleetAutoResumeEnabled -Force:$ForceEnabled)) {
        Write-FleetResumeLog -Once:$FromCycle -Key "$($marker.pid):disabled" -Msg "fleet auto-resume is OFF (fleet_auto_resume setting / MCP_FLEET_AUTORESUME) -- interrupted run (marker pid $($marker.pid)) left for a manual resume"
        return $false
    }
    # A DEAD PID IN THE MARKER DOES NOT MEAN NOTHING IS RUNNING. A coordinator resumed a moment
    # ago -- by start_all's resume_interrupted_fleet.py, or by hand -- writes its fresh marker
    # only after its imports and ledger load, so for that window the marker still names the
    # dead pid. Resuming again then puts two coordinators on one .fleet directory, each
    # overwriting the other's status. start_all skips its resume in the same case (1f4588a,
    # Get-FleetResumeSkipReason); this is the supervisor's side of that rule.
    $runningCoordinators = @(Get-ThisCheckoutFleetCoordinatorPids | Where-Object { $_ })
    if ($runningCoordinators.Count -gt 0) {
        Write-FleetResumeLog -Once:$FromCycle -Key "$($marker.pid):live" -Msg ("fleet run marker names dead pid $($marker.pid), but a fleet coordinator of this checkout " +
                   "is already running (pid " + ($runningCoordinators -join ", ") + ") -- not resuming a second one")
        return $false
    }
    $freeNow = $null
    $signature = ""
    $enospc = $false
    if ($snap) {
        # THE LOOP GUARD, snapshot source only. The floor is READ (settings.txt via the
        # product's own accessor) and never chosen here; no value is defined in this script.
        try { $freeNow = [int64](New-Object System.IO.DriveInfo ([System.IO.Path]::GetPathRoot($FleetDir))).AvailableFreeSpace } catch { $freeNow = $null }
        $floorGb = $null
        try {
            $f = & $Py -c "import sys; sys.path.insert(0, r'$Root'); from relay.fleet_runner import settings_disk_floor; print(settings_disk_floor())" 2>$null
            $parsed = 0.0
            if ($f -and [double]::TryParse(([string]$f).Trim(), [System.Globalization.NumberStyles]::Float, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$parsed) -and $parsed -gt 0) { $floorGb = $parsed }
        } catch { $floorGb = $null }
        try {
            $sigOut = & $Py -c "import sys, json; sys.path.insert(0, r'$Root'); sys.path.insert(0, r'$Root\scripts\win'); import resume_interrupted_fleet as r; d = r.fleet_resume_record(r'$($snap.Path)'); s, e = r.signature_for(r'$FleetDir', d, d.get('marker') or {}); print(s + ' ' + str(int(e)))" 2>$null
            $parts = ([string]$sigOut).Trim() -split ' '
            if ($parts.Count -ge 1 -and $parts[0]) { $signature = $parts[0] }
            if ($parts.Count -ge 2 -and $parts[1] -eq "1") { $enospc = $true }
        } catch { }
        $reason = Get-FleetResumeGate -Record $snap.Data -Now 0 -FreeBytes $freeNow -FloorGb $floorGb `
            -Signature $signature -CoordinatorLive:($runningCoordinators.Count -gt 0) -Enospc:$enospc
        if ($reason -ne "ok") {
            Write-FleetResumeLog -Once:$FromCycle -Key "$($marker.pid):refused:$reason" -Msg "fleet auto-resume REFUSED for pid $($marker.pid): $reason (free bytes $freeNow)"
            if (-not $DryRun) {
                # persist the refusal in the snapshot (max_resumes flips it to gave_up)
                try {
                    & $Py -c "import sys, time; sys.path.insert(0, r'$Root'); from relay import fleet_resume; fleet_resume.record_blocked(r'$($snap.Path)', time.time(), '$reason', $(if ($null -ne $freeNow) { $freeNow } else { 'None' }), '$signature')" 2>$null | Out-Null
                } catch { }
            }
            return $false
        }
    }
    Update-PythonInterpreter "the fleet auto-resume"
    $resumeArgs = @()
    if ($marker.resume_argv) { $resumeArgs = @($marker.resume_argv) }
    $resumeArgs = @($resumeArgs) + "--resume"
    $shown = ($resumeArgs -join " ")
    Write-FleetResumeLog -Once:($FromCycle -and $DryRun) -Key "$($marker.pid):dry" -Msg "fleet run INTERRUPTED (marker pid $($marker.pid) is dead) -> auto-resuming: python -m relay.fleet_runner $shown"
    if ($DryRun) {
        Write-FleetResumeLog -Once:($FromCycle -and $DryRun) -Key "$($marker.pid):dry2" -Msg "fleet auto-resume DRY RUN -- not relaunching (verification mode)"
        return $true
    }
    try {
        # -PASSTHRU, SAME REASON AS Start-Server's. Without it the launch is fire-and-forget
        # and a runner that dies before argparse (docs/agent_contract.md's own example: a bad
        # path, the wrong interpreter, a failed import) leaves literally nothing behind --
        # not even a marker, since that is written from inside main() and this death happens
        # before main() runs. Registering it here is what lets Invoke-AutoResumeRunnerCheck
        # (see its header, above the marker-path variables) read the exit code back later.
        $fleetLaunchAt = Get-Date
        # WHICH INTERRUPTED RUN THIS ONE DESCENDS FROM, so a run that dies again keeps the
        # resume count (relay.fleet_reaper inherits it from the marker's resume_lineage).
        $hadLineage = Test-Path Env:MCP_FLEET_RESUME_LINEAGE
        if ($snap -and $snap.Data.run_id) { $env:MCP_FLEET_RESUME_LINEAGE = [string]$snap.Data.run_id }
        # THE LAUNCH GUARD (relay.fleet_resume.autostart_hold), WRITTEN BEFORE THE LAUNCH with no pid
        # yet and completed with the pid below. Until the resumed coordinator has written its own
        # marker nothing says "a fleet is coming up", and a queue pass (this cycle's drain, or the
        # server handing a goal over) would start a fresh coordinator for the queued goals while
        # the interrupted run is still waiting. The interrupted run goes first.
        $guardRun = ""
        if ($snap -and $snap.Data.run_id) { $guardRun = ([string]$snap.Data.run_id) -replace '[^A-Za-z0-9_\-]', '' }
        try {
            & $Py -c "import sys, time; sys.path.insert(0, r'$Root'); from relay import fleet_resume; fleet_resume.write_launch_guard(r'$FleetDir', 0, '$guardRun', time.time())" 2>$null | Out-Null
        } catch { }
        try {
            $fleetProc = Start-Process -FilePath $Py -ArgumentList (@("-m", "relay.fleet_runner") + $resumeArgs) `
                -WorkingDirectory $Root -WindowStyle Hidden -PassThru
        } finally {
            if (-not $hadLineage) { Remove-Item Env:MCP_FLEET_RESUME_LINEAGE -ErrorAction SilentlyContinue }
        }
        if ($snap -and $fleetProc) {
            try {
                & $Py -c "import sys, time; sys.path.insert(0, r'$Root'); from relay import fleet_resume; fleet_resume.record_resume(r'$($snap.Path)', time.time(), $(if ($null -ne $freeNow) { $freeNow } else { 'None' }), '$signature')" 2>$null | Out-Null
            } catch { }
        }
        # complete the guard with the pid (or drop it when nothing was started)
        try {
            if ($fleetProc) {
                & $Py -c "import sys, time; sys.path.insert(0, r'$Root'); from relay import fleet_resume; fleet_resume.write_launch_guard(r'$FleetDir', $($fleetProc.Id), '$guardRun', time.time())" 2>$null | Out-Null
            } else {
                & $Py -c "import sys; sys.path.insert(0, r'$Root'); from relay import fleet_resume; fleet_resume.clear_launch_guard(r'$FleetDir')" 2>$null | Out-Null
            }
        } catch { }
        Register-AutoResumeRunner -Proc $fleetProc -LaunchTime $fleetLaunchAt -Kind "fleet" `
            -CommandLine ('"' + $Py + '" -m relay.fleet_runner ' + $shown)
        if ($fleetProc) { $script:ResumedFleet = @{ Proc = $fleetProc; OldPid = [int]$marker.pid } }
        Write-Log "fleet coordinator relaunched with --resume"
        try {
            & $Py -c "import sys; sys.path.insert(0, r'$Root'); from tools.notify_ops import notify_desktop; notify_desktop('Fleet auto-resumed', 'An interrupted overnight fleet run was detected after startup and relaunched with --resume.')" 2>$null | Out-Null
        } catch { }
        return $true
    } catch {
        Write-Log "fleet auto-resume FAILED to relaunch: $($_.Exception.Message)"
        try {
            & $Py -c "import sys; sys.path.insert(0, r'$Root'); from relay import fleet_resume; fleet_resume.clear_launch_guard(r'$FleetDir')" 2>$null | Out-Null
        } catch { }
        return $false
    }
}

function Test-ReviewAutoResumeEnabled {
    $v = $env:MCP_REVIEW_AUTORESUME
    if ([string]::IsNullOrWhiteSpace($v)) { return $true }
    return -not ($v -in @("0", "false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF"))
}

function Get-ReviewActiveMarker {
    if (-not (Test-Path $ReviewMarkerPath)) { return $null }
    try {
        return (Get-Content -Path $ReviewMarkerPath -Raw -ErrorAction Stop) |
            ConvertFrom-Json -ErrorAction Stop
    } catch { return $null }
}

function Test-ReviewMarkerProcessAlive {
    param($Marker)
    $procId = 0
    $markerStarted = 0.0
    try {
        $procId = [int]$Marker.pid
        $markerStarted = [double]$Marker.started
    } catch { return $false }
    if ($procId -le 0) { return $false }
    $process = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $false }
    # A PID can be reused after a reboot. The real coordinator necessarily started no
    # later than its marker; a newer process with the same PID must not suppress recovery.
    try {
        $processStarted = [DateTimeOffset]::new($process.StartTime).ToUnixTimeSeconds()
        if ($markerStarted -gt 0 -and $processStarted -gt ($markerStarted + 60)) {
            return $false
        }
        if ($process.ProcessName -notlike "python*") { return $false }
    } catch { return $false }
    return $true
}

function Invoke-ReviewAutoResume {
    # Unlike the legacy fleet marker, check this on EVERY supervisor cycle. A multi-hour
    # LOCAL_LOOP pipeline can lose its coordinator without a reboot; waiting for the next
    # Windows startup would defeat overnight resilience.
    if (-not (Test-ReviewAutoResumeEnabled)) { return $false }
    $marker = Get-ReviewActiveMarker
    if ($null -eq $marker) { return $false }
    $procId = 0
    try { $procId = [int]$marker.pid } catch { return $false }
    if (Test-ReviewMarkerProcessAlive $marker) { return $false }
    try {
        $retryAfter = [double]$marker.retry_after
        $nowEpoch = [DateTimeOffset]::Now.ToUnixTimeSeconds()
        if ($retryAfter -gt $nowEpoch) { return $false }
    } catch { }
    $resumeArgs = @($marker.resume_argv)
    if ($resumeArgs.Count -eq 0) {
        Write-Log "review auto-resume skipped: marker has no resume_argv"
        return $false
    }
    Update-PythonInterpreter "the review auto-resume"
    $key = "$($marker.started)|$($marker.stamp)|$($marker.restart_count)"
    $since = ((Get-Date) - $script:LastReviewResumeAttempt).TotalSeconds
    if ($key -eq $script:LastReviewResumeKey -and $since -lt 300) { return $false }
    $script:LastReviewResumeKey = $key
    $script:LastReviewResumeAttempt = Get-Date
    $shown = $resumeArgs -join " "
    Write-Log "review pipeline INTERRUPTED (marker pid $procId is dead) -> auto-resuming: python -m bench.review_run $shown"
    try {
        # -PASSTHRU -- see Invoke-FleetAutoResume's identical comment above; the same silent-
        # death-before-argparse risk applies to bench.review_run.
        $reviewLaunchAt = Get-Date
        $reviewProc = Start-Process -FilePath $Py -ArgumentList (@("-m", "bench.review_run") + $resumeArgs) `
            -WorkingDirectory $Root -WindowStyle Hidden -PassThru
        Register-AutoResumeRunner -Proc $reviewProc -LaunchTime $reviewLaunchAt -Kind "review" `
            -CommandLine ('"' + $Py + '" -m bench.review_run ' + $shown)
        Write-Log "review pipeline relaunched from durable stamp $($marker.stamp)"
        return $true
    } catch {
        Write-Log "review auto-resume FAILED to relaunch: $($_.Exception.Message)"
        return $false
    }
}


function Test-LocalLoopAutoResumeEnabled {
    $v = $env:MCP_LOCAL_LOOP_AUTORESUME
    if ([string]::IsNullOrWhiteSpace($v)) { return $true }
    return -not ($v -in @("0", "false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF"))
}

function Test-LocalLoopMarkerProcessAlive {
    param($Marker)
    $procId = 0
    $markerStarted = 0.0
    try {
        $procId = [int]$Marker.pid
        $markerStarted = [double]$Marker.started
    } catch { return $false }
    if ($procId -le 0) { return $false }
    $process = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $false }
    try {
        $processStarted = [DateTimeOffset]::new($process.StartTime).ToUnixTimeSeconds()
        # Bind the marker to this process birth, not merely to a live/reused PID.  A different
        # python that started long before OR after the marker must not suppress recovery forever.
        if ($markerStarted -gt 0 -and [Math]::Abs($processStarted - $markerStarted) -gt 60) { return $false }
        if ($process.ProcessName -notlike "python*") { return $false }
    } catch { return $false }
    return $true
}

function Write-LocalLoopMarkerAtomic {
    param([string]$Path, $Marker)
    try {
        $dir = Split-Path -Parent $Path
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $tmp = $Path + ".tmp-" + $PID + "-" + [Guid]::NewGuid().ToString("N")
        $json = $Marker | ConvertTo-Json -Depth 8 -Compress
        [IO.File]::WriteAllText($tmp, $json, (New-Object Text.UTF8Encoding($false)))
        Move-Item -Path $tmp -Destination $Path -Force
        return $true
    } catch {
        try { if ($tmp -and (Test-Path $tmp)) { Remove-Item $tmp -Force } } catch { }
        return $false
    }
}

function Get-LocalLoopRetryDelaySeconds {
    param([int]$RestartCount)
    # Back off crash loops without making a previously-stable long job slow to recover.  The
    # child preserves retry_after; if it survives past the deadline, a later crash resumes
    # immediately because that deadline is already in the past.
    $n = [Math]::Max(1, $RestartCount)
    $exp = [Math]::Min(10, $n - 1)
    $delay = [int](30 * [Math]::Pow(2, $exp))
    return [int][Math]::Min(900, $delay)
}


function Invoke-LocalLoopAutoResume {
    # Generic Cockpit LOCAL_LOOP jobs write one marker per job. Check EVERY cycle: the point of
    # this runtime is multi-hour work, so waiting for a reboot to recover a dead coordinator is
    # not acceptable. The controller itself also holds a per-job kernel lock; this supervisor is
    # a relauncher, not the final exclusion layer.
    if (-not (Test-LocalLoopAutoResumeEnabled)) { return $false }
    if (-not (Test-Path $LocalLoopMarkerDir)) { return $false }
    $did = $false
    foreach ($file in @(Get-ChildItem -Path $LocalLoopMarkerDir -Filter "*.json" -File -ErrorAction SilentlyContinue)) {
        $marker = $null
        try { $marker = (Get-Content -Path $file.FullName -Raw -ErrorAction Stop) | ConvertFrom-Json -ErrorAction Stop }
        catch { continue }
        if ($null -eq $marker) { continue }
        if (Test-LocalLoopMarkerProcessAlive $marker) { continue }
        $nowEpoch = [DateTimeOffset]::Now.ToUnixTimeSeconds()
        try { if ([double]$marker.retry_after -gt $nowEpoch) { continue } } catch { }
        $resumeArgs = @($marker.resume_argv)
        if ($resumeArgs.Count -eq 0) {
            Write-Log "LOCAL_LOOP auto-resume skipped for $($file.Name): marker has no resume_argv"
            continue
        }
        Update-PythonInterpreter "the LOCAL_LOOP auto-resume"
        $jobId = [string]$marker.job_id
        $shown = $resumeArgs -join " "
        try { $restart = [int]$marker.restart_count + 1 } catch { $restart = 1 }
        $retryDelay = Get-LocalLoopRetryDelaySeconds -RestartCount $restart

        # Reserve this retry BEFORE spawning, but do not claim controller ownership here.
        # The supervisor never rewrites pid/started ownership: only a controller that actually
        # wins the per-job kernel lock may publish its process identity. This avoids both the
        # fast-child race (child writes a newer marker before Start-Process returns) and the
        # false-death race (a duplicate child loses the lock but would otherwise steal marker
        # ownership from the still-live controller).
        $marker | Add-Member -NotePropertyName restart_count -NotePropertyValue $restart -Force
        $marker | Add-Member -NotePropertyName retry_after -NotePropertyValue ($nowEpoch + $retryDelay) -Force
        if (-not (Write-LocalLoopMarkerAtomic -Path $file.FullName -Marker $marker)) {
            Write-Log "LOCAL_LOOP auto-resume skipped for '$jobId': could not persist retry/backoff marker"
            continue
        }

        try {
            $launchAt = Get-Date
            $proc = Start-Process -FilePath $Py -ArgumentList (@("-m", "relay.local_loop_controller") + $resumeArgs) `
                -WorkingDirectory $Root -WindowStyle Hidden -PassThru
            # A healthy child takes the per-job lock and then rewrites pid/started itself while
            # preserving the restart_count/retry_after deadline reserved above.
            Register-AutoResumeRunner -Proc $proc -LaunchTime $launchAt -Kind ("local-loop:" + $jobId) `
                -CommandLine ('"' + $Py + '" -m relay.local_loop_controller ' + $shown)
            Write-Log "LOCAL_LOOP job '$jobId' INTERRUPTED -> relaunched pid $($proc.Id) from $($file.Name)"
            $did = $true
        } catch {
            # The backoff reservation is already durable. Do not alter pid/started here: the
            # old dead owner remains evidence until a future lock-winning controller replaces it.
            Write-Log "LOCAL_LOOP auto-resume FAILED for '$jobId' (retry in ${retryDelay}s): $($_.Exception.Message)"
        }
    }
    return $did
}

function Invoke-LocalLoopCampaignDrain {
    # Campaign intake is durable before a controller exists.  A short enqueue process normally
    # launches its children immediately, but if it dies in that gap the active manifest remains.
    # This pass makes the supervisor the recovery owner for those marker-less READY jobs.
    # Do not duplicate Python's .env parser here. The controller owns the feature flag and loads
    # .env; this supervisor only avoids spawning a drain when no active campaign exists.
    if (-not (Test-Path $LocalLoopCampaignPath)) { return $false }
    Update-PythonInterpreter "the LOCAL_LOOP campaign drain"
    $out = ""
    $code = 0
    try {
        Push-Location $Root
        try {
            $out = (& $Py -W ignore -m relay.local_loop_controller --drain-campaign --state-dir $FleetDir) -join ""
            $code = $LASTEXITCODE
        } finally {
            Pop-Location
        }
    } catch {
        Write-Log "LOCAL_LOOP campaign drain FAILED: $($_.Exception.Message)"
        return $false
    }
    if ($code -ne 0) {
        Write-Log "LOCAL_LOOP campaign drain exit $code"
        return $false
    }
    if (-not [string]::IsNullOrWhiteSpace($out)) {
        try {
            $result = $out | ConvertFrom-Json -ErrorAction Stop
            $launched = @($result.launched)
            if ($launched.Count -gt 0) {
                Write-Log ("LOCAL_LOOP campaign launched " + $launched.Count + " queued job(s): " + ($launched -join ", "))
            }
        } catch {
            Write-Log "LOCAL_LOOP campaign drain returned unreadable output: $out"
        }
    }
    return $true
}

# -- Queue delivery: the reaper + router pass, and the wait between ticks -----------------------
# The two steps the tick has always run back to back, now callable from two places: the full
# tick (unchanged position and order) and the express pass inside Wait-ForNextTick below.
# THE REAP MUST NOT DELETE THE MARKER OF A RUN THAT IS BEING RESUMED. The reaper removes a
# marker whose pid is dead -- and a marker whose pid is dead is exactly what a resume reads and
# leaves in place until the resumed coordinator writes its own, after its imports and ledger
# load: tens of seconds, i.e. several reap passes (every tick, and every express pass in
# Wait-ForNextTick). Reaping inside that window finalises status/history of a run that is being
# continued, and if the resumed coordinator then dies before writing its marker, the record
# that the run was interrupted is gone and no later boot can resume it.
#
# A GUARD, NOT AN ORDERING. The resume must come BEFORE any reap (the reap deletes the file the
# resume reads), and the window is after the resume and longer than a tick, so no order of the
# first loop closes it. The reap is withheld while the marker names a dead pid AND a resumer of
# it is alive: the coordinator this supervisor launched ($script:ResumedFleet), or any
# relay.fleet_runner of this checkout started with --resume (start_all's
# resume_interrupted_fleet.py, or a person). It lifts itself as soon as the marker names a live
# pid (the resumed run wrote its own; the reaper leaves that alone anyway), or the resumer has
# exited (then the marker is again an interrupted run's, and Invoke-AutoResumeRunnerCheck
# reports how the runner ended).
$script:FleetReapHeldFor = ""

function Get-FleetReapHoldReason {
    # "" when the reap may run, else who it is being withheld for.
    $marker = Get-FleetActiveMarker
    if ($null -eq $marker) { $script:ResumedFleet = $null; return "" }
    $markerPid = 0
    try { $markerPid = [int]$marker.pid } catch { $markerPid = 0 }
    if ($markerPid -gt 0 -and (Test-PidAlive -ProcId $markerPid)) { $script:ResumedFleet = $null; return "" }
    $holders = @()
    if ($script:ResumedFleet) {
        $gone = $true
        try { $script:ResumedFleet.Proc.Refresh(); $gone = $script:ResumedFleet.Proc.HasExited } catch { $gone = $true }
        if ($gone) { $script:ResumedFleet = $null }
        else { $holders += ("pid " + $script:ResumedFleet.Proc.Id + " (resumed by this supervisor)") }
    }
    # One process-table scan (the shared detection), then a per-pid read of the few it found.
    # Reached only while a dead-pid marker exists, never on an ordinary tick.
    foreach ($coordPid in @(Get-ThisCheckoutFleetCoordinatorPids | Where-Object { $_ })) {
        if ($script:ResumedFleet -and $coordPid -eq $script:ResumedFleet.Proc.Id) { continue }
        $cl = ""
        try { $cl = [string](Get-CimInstance Win32_Process -Filter "ProcessId=$coordPid" -ErrorAction Stop).CommandLine } catch { }
        if ($cl -match '(^|\s)--resume(\s|$)') {
            $holders += ("pid " + $coordPid + " (a --resume coordinator of this checkout)")
        }
    }
    if ($holders.Count -eq 0) { return "" }
    return ("marker pid $markerPid is dead but its run is being resumed by " + ($holders -join ", "))
}

function Invoke-FleetReap {
    # Clear phantom fleet runs whose coordinator process died without a supervisor restart
    # (Invoke-FleetAutoResume above only runs once at supervisor startup, so a mid-session
    # coordinator kill/crash would otherwise leave .fleet/status.json stuck showing
    # running=true forever). Best-effort, idempotent, never relaunches anything -- see
    # relay/fleet_reaper.py.
    #
    # THE FLEET DIRECTORY IS PASSED, NOT LEFT TO THE WORKING DIRECTORY. reap_stale_run()
    # defaults to the RELATIVE ".fleet", and python inherits this process's working directory
    # -- and the logon launcher (start_background_hidden.vbs) sets CurrentDirectory to
    # scripts\, where there is no .fleet, before starting start_all, which starts this script
    # without -WorkingDirectory. Consistent with that, this machine's supervisor log
    # (2026-09-24, 1698 lines) shows exactly one reap, on 2026-09-09.
    $hold = Get-FleetReapHoldReason
    if ($hold) {
        if ($hold -ne $script:FleetReapHeldFor) { Write-Log "stale-run reap withheld: $hold" }
        $script:FleetReapHeldFor = $hold
        return
    }
    $script:FleetReapHeldFor = ""
    try {
        $reapOut = & $Py -c "import sys; sys.path.insert(0, r'$Root'); from relay.fleet_reaper import reap_stale_run; import json; r = reap_stale_run(r'$FleetDir'); print(json.dumps(r) if r else '')" 2>$null
        if ($reapOut) { Write-Log "reaped stale fleet run: $reapOut" }
    } catch { }
}

function Invoke-QueueDrain {
    # Drain the typed-job queue. An agent reaching the MCP server can hand this machine a goal
    # (tools/fleet_intake.fleet_submit), and until something calls the router that goal just
    # sits in .fleet/tasks/pending -- which is where the first real submission sat.
    #
    # ONE PASS FROM THIS LOOP, NOT A DAEMON. The router's own --poll-s mode would be a second
    # long-lived process to start, supervise and reap; this loop already runs, already survives
    # for days. Fewer moving parts is the whole reason. Latency is handled by the express pass
    # in Wait-ForNextTick, which calls THIS function -- still one deliverer, one process.
    #
    # Unattended is safe by construction rather than by promise: a LOCAL job still meets the
    # approval gate (default mode confirms every first-seen class into awaiting/), a CLAUDE job
    # is only written out, and a FLEET goal joins a run that is ALREADY in flight -- nothing
    # here starts one, so no browser opens and no Copilot budget is spent by a queue drain.
    # NO 2>$null ON A NATIVE COMMAND. Windows PowerShell wraps a native process's stderr lines
    # in ErrorRecords, so redirecting them turns a harmless import-time DeprecationWarning
    # into a terminating error. -W ignore keeps the warning down; stderr is left alone.
    #
    # THE PATH IS BUILT IN TWO SEGMENTS, and that is not style. Written as one string it was
    # "relay\task_router.py", and the \t in it became a TAB before it ever reached this file
    # -- so the supervisor spent every 15-second pass launching python against
    # "relay<TAB>ask_router.py", getting exit code 2 and an empty stdout, and saying nothing.
    # The queue never moved and nothing anywhere reported a failure. Two segments cannot carry
    # an escape, so the bug cannot come back the same way.
    #
    # $LASTEXITCODE IS CHECKED. The router failing is not a PowerShell exception, so the catch
    # below never saw it; a drain that fails quietly is exactly how this went unnoticed.
    param([string]$Tag = "")
    $label = "task_router"
    if ($Tag) { $label = "task_router ($Tag)" }
    try {
        $rtPath = Join-Path (Join-Path $Root "relay") "task_router.py"
        $routed = (& $Py -W ignore $rtPath --once) -join ""
        if ($LASTEXITCODE -ne 0) {
            Write-Log "${label}: exit $LASTEXITCODE from $rtPath (queue not drained)"
        } elseif ($routed -and $routed.Trim() -and $routed.Trim() -ne "[]") {
            Write-Log "${label}: $($routed.Trim())"
        }
    } catch {
        Write-Log "$label pass failed: $($_.Exception.Message)"
    }
}

$PendingDir = Join-Path (Join-Path (Join-Path $Root ".fleet") "tasks") "pending"

function Get-PendingJobNames {
    # File NAMES only, read in-process: no python, no child process, nothing parsed. fleet_submit
    # writes <id>.json.tmp and os.replace()s it into place, so a name ending in .json is a
    # finished file; the extra EndsWith keeps a .json.tmp (or an 8.3 alias) from counting.
    param([string]$Dir)
    $names = New-Object System.Collections.Generic.List[string]
    try {
        foreach ($f in [System.IO.Directory]::GetFiles($Dir, "*.json")) {
            $n = [System.IO.Path]::GetFileName($f)
            if ($n.EndsWith(".json", [StringComparison]::OrdinalIgnoreCase)) { $names.Add($n) }
        }
    } catch { }
    return ,$names
}

# WHICH PENDING FILES A ROUTER PASS HAS ALREADY BEEN SHOWN. A job can stay in pending after a
# pass on purpose -- still owned by a live writer, held back by a backoff, waiting on something
# the router decided to wait for. Triggering on "pending is not empty" would then run an
# express pass every few seconds for as long as that job waits, i.e. the router every 3 s
# forever instead of every 15. Triggering on "a NAME appeared that no pass has seen" fires once
# per arrival and never again for the same file. Names are added from a snapshot taken BEFORE
# each pass (full or express), so a file landing mid-pass is still new afterwards; names that
# have left pending are pruned on every probe, so the set cannot grow without bound, and a job
# that leaves and is later put back counts as a new arrival, which it is.
$script:ExpressSeen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
$script:LastExpressPassAt = [datetime]::MinValue

function Wait-ForNextTick {
    # REPLACES `Start-Sleep -Seconds $IntervalSeconds`. Measured 2026-09-24: a job written by
    # fleet_submit waited ~26 s before the router saw it -- the rest of a ~20 s tick plus the
    # sleep. This waits the same interval, but looks at pending once a second and, when a job
    # file appears that no pass has been shown, runs an EXPRESS PASS at once: the reaper and
    # then the router, the same two steps in the same order as the full tick
    # ($ExpressPass is { Invoke-FleetReap; Invoke-QueueDrain }). The reaper is never skipped,
    # including while a server restart is in progress -- queue delivery does not depend on the
    # server, and a delivery pass that skipped the reaper could hand a goal to a run that is
    # already dead.
    #
    # WHY A 1-SECOND PROBE AND NOT A NAMED EVENT that fleet_submit signals. An event is a
    # second channel into the supervisor: it needs an ACL that lets the server's user signal
    # it and nobody else, a name in the Global/Local namespace that another process can create
    # first (squatting) and so either block or spoof the wake-up, and a lost-wakeup story for
    # a signal raised while the supervisor is busy in the tick or not running at all -- which
    # ends in polling the directory anyway. The probe is a directory listing in this process:
    # no child process, no new resident memory, nothing another process can hold or forge,
    # and at worst one second of latency.
    #
    # THE SUPERVISOR STAYS THE ONLY DELIVERER. The express pass runs here, synchronously, in the
    # one supervisor that holds the Global mutex; it is not a second process and cannot overlap
    # the tick's own pass.
    #
    # RATE-LIMITED ($MinExpressSpacingSeconds, 3 s). A burst of submissions gets one pass for
    # everything that arrived inside the window, not one router process per file. A new name
    # that arrives inside the window is not marked seen, so it triggers the first probe after
    # the window closes.
    #
    # AN ABSOLUTE DEADLINE, fixed when the wait starts. Express passes never extend it: a
    # steady stream of submissions must not postpone the health, stale-code and tunnel checks
    # of the full tick indefinitely. A pass already running at the deadline finishes; nothing
    # new starts after it.
    param(
        [double]$Seconds,
        [string]$Dir,
        [scriptblock]$ExpressPass,
        [double]$ProbeSeconds = 1,
        [double]$MinExpressSpacingSeconds = 3
    )
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ($true) {
        $leftMs = ($deadline - (Get-Date)).TotalMilliseconds
        if ($leftMs -le 0) { return }
        Start-Sleep -Milliseconds ([int][math]::Ceiling([math]::Min($ProbeSeconds * 1000, $leftMs)))
        if ((Get-Date) -ge $deadline) { return }
        $names = Get-PendingJobNames -Dir $Dir
        $present = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
        foreach ($n in $names) { [void]$present.Add($n) }
        $script:ExpressSeen.IntersectWith($present)
        $fresh = 0
        foreach ($n in $names) { if (-not $script:ExpressSeen.Contains($n)) { $fresh++ } }
        if ($fresh -eq 0) { continue }
        if (((Get-Date) - $script:LastExpressPassAt).TotalSeconds -lt $MinExpressSpacingSeconds) { continue }
        $script:LastExpressPassAt = Get-Date
        foreach ($n in $names) { [void]$script:ExpressSeen.Add($n) }
        Write-Log "express queue pass: $fresh new job file(s) in pending"
        try { & $ExpressPass } catch { Write-Log "express queue pass failed: $($_.Exception.Message)" }
    }
}

Write-Log "supervisor up (tunnel=$TunnelName port=$Port interval=${IntervalSeconds}s debounce=$FailuresBeforeAction)"

# Checked once, here, before the forever health-check loop starts.
Invoke-FleetAutoResume -DryRun:$FleetResumeDryRun | Out-Null
Invoke-ReviewAutoResume | Out-Null
Invoke-LocalLoopCampaignDrain | Out-Null
Invoke-LocalLoopAutoResume | Out-Null

# THE DEBOUNCE IS FOR A SERVER THAT MIGHT COME BACK, NOT FOR ONE THAT WAS NEVER STARTED.
# Starting at zero meant the FIRST launch waited out four consecutive failures. MEASURED on
# this machine: "supervisor up" at 11:44:39, "MCP server process launched" at 11:45:52 -- 73
# seconds, and the ten runs before it were all 65-75s. start_all.ps1 launches only this
# supervisor (it never runs main.py itself) and quickstart runs doctor as soon as start_all
# returns, so on every fresh machine the health check ran inside a window where the server did
# not yet exist BY DESIGN: server down, tunnel not serving, Bearer rejected -- one cause, and
# none of them a fault.
#
# THE CONDITION IS "NOTHING IS LISTENING", NOT "THE FIRST CHECK FAILED". Priming the counter
# instead would let one transient /health timeout fire Start-Server against a server that is
# perfectly healthy -- and Start-Server kills whatever owns the port (see its first act). When
# no process owns the port there is nothing to kill and nothing to protect, so launching at
# once is safe; in every other case the debounce still does its job.
$serverMiss = 0
# A FAILED QUERY IS NOT AN EMPTY ANSWER. Catching the exception into $null made 'the port
# could not be inspected' indistinguishable from 'nothing owns it' -- and the branch below
# calls Start-Server, whose first act is to kill whatever owns the port. An inspection that
# fails must not license that; the debounce is the correct behaviour when we cannot tell.
#
# AND AN EMPTY ANSWER IS NOT A FAILED QUERY EITHER: Get-NetTCPConnection throws ObjectNotFound
# when nothing listens, which the plain catch here used to read as "could not inspect", so this
# launch never happened (see Get-PortListenerPids). The helper tells the two apart.
$portQueried = $false
$portOwner = $null
$startupListeners = Get-PortListenerPids
if ($startupListeners.Queried) {
    $portQueried = $true
    if (@($startupListeners.Pids).Count -gt 0) { $portOwner = $startupListeners.Pids }
}
if ($portQueried -and -not $portOwner) {
    Write-Log "nothing is listening on :$Port at startup -> launching the server now, without the debounce"
    Start-Server
}
$tunnelMiss = 0
$loggedIn = $null   # tri-state ($null unknown / $true / $false) -- log only on transition

# -- TUNNEL STARTUP FAST PATH (begin) --------------------------------------------------------
# THE SAME RULE AS THE SERVER'S, JUST ABOVE, APPLIED TO THE TUNNEL. Invoke-TunnelHostingCheck's
# debounce (FailuresBeforeAction misses before Start-TunnelHost, see its "none" branch) is
# correct for a LATER tick, where a miss might be a transient blip in a tunnel that was working.
# It is not correct for the very FIRST tick: at supervisor START, "none" means nothing of ours
# already hosts this tunnel, so there is nothing a fast re-host could knock over (unlike
# Start-Server, whose first act is to kill whatever owns the port -- see the block above, and
# why IT needed the same "nothing to protect" reasoning before it could skip its own debounce).
#
# MEASURED after a reboot (2026-09-24 11:33): the supervisor waited through four failed "tunnel
# host connections = 0" checks -- about 80s at this loop's pace -- before hosting the tunnel at
# all, even though nothing of ours was hosting it the moment this process started.
#
# ONLY "none" SKIPS THE DEBOUNCE. "foreign" and "shared" are never fought, at startup or any
# other tick -- Resolve-TunnelHostingState already tells those apart from "none" correctly, so
# this block only has to call it once and branch on the one state that means "go ahead".
# $script:ForeignStreak is left at its initial 0 so a foreign/shared state discovered here still
# goes through the loop's own $FailuresBeforeAction-gated report exactly as it would starting
# from any other tick -- this fast path changes nothing about foreign/shared handling.
if (Test-DevtunnelLoggedInCached) {
    $startupConn = Get-TunnelHostConnections
    $startupOurHost = $false
    $startupLocalPid = $null
    $startupTunnelPid = $null
    if ($null -ne $startupConn -and $startupConn -ge 1) {
        # Same probes Invoke-TunnelHostingCheck makes, only when connections might be ours.
        $startupOurHost = Test-OurTunnelHostRunning
        $startupLocalPid = Get-HealthPidAt "http://127.0.0.1:$Port/health" 5000
        $startupTunnelPid = Get-TunnelServerPidBounded
    }
    $startupTunnelState = Resolve-TunnelHostingState -Connections $startupConn -OurHostRunning $startupOurHost `
                                                      -LocalPid $startupLocalPid -TunnelPid $startupTunnelPid
    Set-TunnelHostStatus -State $startupTunnelState -Connections $startupConn -OurHostRunning $startupOurHost `
                         -LocalPid $startupLocalPid -TunnelPid $startupTunnelPid
    # Seeds the tick loop's own state tracking so its first "was this contested before" check
    # (Invoke-TunnelHostingCheck's $wasContested) starts from what is actually true right now,
    # instead of from "" (which would read as "never contested" even if it already is).
    $script:TunnelState = $startupTunnelState
    if ($startupTunnelState -eq "none") {
        Write-Log "tunnel host connections = 0 at startup -> hosting the tunnel now, without the debounce"
        if (-not (Start-TunnelHost)) {
            Clear-DevtunnelLoginCache "the startup re-host did not establish"
        }
    }
} else {
    # Not logged in at startup: do nothing to devtunnel (see Test-DevtunnelLoggedIn's own
    # comment on why touching it while logged out is unsafe). Setting $loggedIn here, rather
    # than leaving it $null, means the loop's first tick does not print a second "NOT logged
    # in" line for the same fact this already established.
    $loggedIn = $false
}
# -- TUNNEL STARTUP FAST PATH (end) ----------------------------------------------------------

while ($true) {
    # Live self-correction ("never again" / defense-in-depth): if .env's MCP_TUNNEL_NAME
    # changed since this supervisor started hosting $TunnelName -- e.g. heal_tunnel.ps1's
    # self-heal repointed .env to this account's own tunnel while this supervisor was
    # already hosting a stale/borrowed one from a copied .env -- stop the OLD host and
    # switch to the new name. This makes the running supervisor self-correct even if
    # nothing ever re-runs start_all.bat (which also detects and restarts this drift, but
    # only at the moment it is invoked). Bare-name compare (ignores the ".cluster" suffix)
    # so a URL-only .env rewrite of the SAME tunnel never causes a needless churn.
    #
    # First, the interpreter every python launch of this tick uses (D2): one Test-Path unless a
    # .venv has appeared that this supervisor is not using yet. See Update-PythonInterpreter.
    Update-PythonInterpreter "this tick's reaper and queue drain"
    $freshTn = Get-EnvTunnelName
    if ($freshTn -and ((Get-BareTunnelName $freshTn) -ne (Get-BareTunnelName $TunnelName))) {
        # A NEW NAME IS A CLAIM, AND IT USED TO BE BELIEVED WITHOUT CHECKING.
        #
        # Measured 2026-09-09: .env's MCP_TUNNEL_NAME changed from the configured name to
        # branch stopped a WORKING host and switched to a tunnel that does not exist, so hosting
        # failed every ~35s for twelve minutes until something rewrote the name back. The switch
        # was the outage: leaving the old host alone would have cost nothing.
        #
        # Every script in this repository that writes MCP_TUNNEL_NAME was audited and none can
        # produce that value; the writer is still unidentified, and the instrument that would
        # have named it (MCP_TRACE_TOOLCALLS) was off. tools/file_ops.py refuses .env to the file
        # tools but says in its own comment that run_python and shell_exec are not covered -- so
        # a stray same-user subprocess writing .env is a live possibility that no producer-side
        # guard can close.
        #
        # THE CONSUMER IS THE PLACE TO CHECK. Guarding each writer means guessing the whole set
        # of writers; guarding here covers every one of them, known or not. The claim is cheap
        # to test -- ask the CLI whether this account owns the tunnel -- and the failure mode is
        # asymmetric: refusing a real rename costs a log line and a retry next cycle, while
        # accepting a bad one costs the tunnel.
        $ownsIt = $null
        try {
            $lst = & $DevTunnel list 2>&1 | Out-String
            if ($LASTEXITCODE -eq 0 -and $lst) {
                $bare = Get-BareTunnelName $freshTn
                foreach ($ln in ($lst -split "`r?`n")) {
                    if ($ln -match '^\s*([a-z0-9][a-z0-9-]+\.[a-z0-9]+)\s') {
                        if ((Get-BareTunnelName $matches[1]) -eq $bare) { $ownsIt = $true; break }
                    }
                }
                if ($null -eq $ownsIt) { $ownsIt = $false }
            }
        } catch { }
        # $null means the listing itself failed (offline, signed out, CLI missing). That is NOT
        # evidence the name is bad, so it must not be treated as a refusal -- fall through and
        # switch, exactly as before. Only a listing that SUCCEEDED and did not contain the name
        # is grounds to refuse.
        if ($ownsIt -eq $false) {
            Write-Log ("tunnel name in .env changed to '$freshTn', which this account does not own" +
                       " -- KEEPING '$TunnelName' and not switching. Fix MCP_TUNNEL_NAME in .env;" +
                       " retrying the check next cycle.")
            $freshTn = ""
        }
    }
    if ($freshTn -and ((Get-BareTunnelName $freshTn) -ne (Get-BareTunnelName $TunnelName))) {
        Write-Log "tunnel name changed in .env ('$TunnelName' -> '$freshTn') -- stopping the old host and switching"
        # Stop only OUR stale host process(es) for the OLD name -- the exact same
        # targeted match Start-TunnelHost uses below. NEVER `Get-Process devtunnel |
        # Stop-Process`: that would reap an interactive `devtunnel login` (see
        # Test-DevtunnelLoggedIn's comment) or any unrelated tunnel the user hosts by hand.
        Get-CimInstance Win32_Process -Filter "Name='devtunnel.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match '\bhost\b' -and $_.CommandLine -match [regex]::Escape($TunnelName) } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        $TunnelName = $freshTn
        $tunnelMiss = 0
        # A different tunnel: whatever was concluded about the old one's host does not carry over.
        $script:ForeignStreak = 0
        $script:TunnelState = ""
        Reset-TunnelProbeBackoff
    }

    # The reaper, then the queue drain -- see Invoke-FleetReap / Invoke-QueueDrain for why each
    # exists. The pending names are snapshotted BEFORE the pass and marked seen after it, so
    # Wait-ForNextTick below does not run an express pass for a job this pass was already shown.
    $shownToPass = Get-PendingJobNames -Dir $PendingDir
    Invoke-FleetReap
    # AFTER the reap, which is what turns a dead coordinator's marker into a pending snapshot.
    # LIVE when the operator's fleet_auto_resume setting is on (the default); the loop guard in
    # Invoke-FleetAutoResume still decides each time. -FleetResumeDryRun logs only, and
    # -FleetCycleResumeLive forces it on over a setting of off (override, no longer needed).
    Invoke-FleetAutoResume -DryRun:$FleetResumeDryRun -FromCycle -ForceEnabled:$FleetCycleResumeLive | Out-Null
    Invoke-QueueDrain
    foreach ($n in $shownToPass) { [void]$script:ExpressSeen.Add($n) }

    Invoke-ReviewAutoResume | Out-Null
    Invoke-LocalLoopCampaignDrain | Out-Null
    Invoke-LocalLoopAutoResume | Out-Null

    # Report any tracked fleet/review/LOCAL_LOOP auto-resume runner that has exited since the last tick.
    # Every tick, not just after a relaunch, because the runner that needs reporting may still
    # be alive on the tick that launched it and only exit (quickly, if it never got past
    # argparse) on the very next one.
    Invoke-AutoResumeRunnerCheck

    if (Test-ServerUp) {
        $serverMiss = 0
        # The planned-transition marker is useful only while the replacement server is absent.
        # Clear any old marker first; Invoke-StaleServerCycle may immediately publish a fresh one.
        Clear-ServerTransition
        Invoke-StaleServerCycle
    } else {
        # DO NOT KILL A SERVER THAT IS STILL STARTING. main.py takes 10-25 seconds just to
        # import (fastmcp alone is ~19s) before it binds, and longer while the machine is
        # busy. A failure whose reason is "nothing is listening" while a main.py from this
        # repo is alive is a server mid-boot, and launching again does not help it -- it kills
        # the one that was nearly ready and restarts the clock. That is a loop that sustains
        # itself: measured 2026-09-15, three kills over nine minutes, each one hitting a
        # process that had not finished starting.
        #
        # The grace is bounded by the PROCESS, not only by time: if no main.py of ours is
        # alive, there is nothing to wait for and the strike counts immediately. So a server
        # that died at launch is still restarted at the usual speed, and only one that is
        # visibly working towards listening is given room.
        $reason = $script:LastServerCheck
        $booting = $false
        if (-not (Test-PortListening)) {
            # Same identity rule as Start-Server's (Test-IsThisCheckoutServerCommandLine), plus
            # the process this supervisor launched, whose command line on a PATH python names
            # no checkout.
            $alive = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                       Where-Object { Test-IsThisCheckoutServerCommandLine ([string]$_.CommandLine) $Root })
            if ($alive.Count -eq 0 -and $script:ServerProc) {
                try { $script:ServerProc.Refresh(); if (-not $script:ServerProc.HasExited) { $alive = @($script:ServerProc) } } catch { }
            }
            if ($alive.Count -gt 0 -and $script:LastLaunchAt -and
                ((Get-Date) - $script:LastLaunchAt).TotalSeconds -lt $StartupGraceSeconds) {
                $booting = $true
            }
        }
        if ($booting) {
            Write-Log ("server not listening yet ({0}); main.py alive {1:N0}s after launch -- waiting, not restarting" -f
                       $reason, ((Get-Date) - $script:LastLaunchAt).TotalSeconds)
        } else {
            $serverMiss++
            Write-Log "server check failed ($serverMiss/$FailuresBeforeAction) -- $reason"
            if ($serverMiss -ge $FailuresBeforeAction) {
                Start-Server
                $serverMiss = 0
                Start-Sleep -Seconds 6
            }
        }
    }

    # A HOST THIS SUPERVISOR LAUNCHED HAS EXITED -> the cached login answer is no longer
    # trusted (see Test-DevtunnelLoggedInCached). A host started by someone else is not
    # tracked; its failure still reaches the cache through the hosting check below.
    if ($script:TunnelHostProc) {
        $hostGone = $true
        try { $script:TunnelHostProc.Refresh(); $hostGone = $script:TunnelHostProc.HasExited } catch { $hostGone = $true }
        if ($hostGone) {
            # SAY WHAT IT SAID. Exit code and last output lines (see Start-TunnelHost), once.
            Write-Log ("the devtunnel host this supervisor launched has exited" + (Get-TunnelHostExitDetail))
            Clear-DevtunnelLoginCache "the devtunnel host this supervisor launched has exited"
            $script:TunnelHostProc = $null
        }
    }

    if (-not (Test-DevtunnelLoggedInCached)) {
        # First-run / token-cleared: pause tunnel management and DO NOT touch devtunnel,
        # so the user can run `devtunnel login` (interactive) without it being reaped.
        if ($loggedIn -ne $false) {
            Write-Log "devtunnel NOT logged in -> tunnel management PAUSED. Run 'devtunnel login' once (interactive); supervisor will not touch devtunnel until then."
            $loggedIn = $false
        }
        $tunnelMiss = 0
    } else {
        if ($loggedIn -eq $false) { Write-Log "devtunnel now logged in -> resuming tunnel management" }
        $loggedIn = $true
        # ours / none (re-host after the debounce) / foreign or shared (another PC serves this
        # PC's tunnel: report, do not fight) -- see Resolve-TunnelHostingState.
        Invoke-TunnelHostingCheck | Out-Null
    }

    # LAST STEP OF THE TICK: is this process still the code on disk? Stat-only unless a file
    # changed; replaces this supervisor (and exits) only when Get-SupervisorRestartVerdict says ok.
    Invoke-SupervisorCodeCycle

    Wait-ForNextTick -Seconds $IntervalSeconds -Dir $PendingDir -ExpressPass {
        Invoke-FleetReap
        Invoke-QueueDrain -Tag "express"
    }
}
