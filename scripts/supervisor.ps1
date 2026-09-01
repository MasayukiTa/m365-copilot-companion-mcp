<#
.SYNOPSIS
  Keeps the MCP server and the Dev Tunnel host alive.

.DESCRIPTION
  Two failure modes are handled:
    1. The MCP server process dies        -> port 8000 stops responding -> restart it
    2. The Dev Tunnel host *silently drops* -> the devtunnel process stays alive but
       "Host connections" falls to 0 -> kill the stale host and re-host.

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
    # Dry-run the fleet coordinator auto-resume check only: log what WOULD happen
    # (marker found, pid dead, would relaunch with these args) without actually
    # starting a process. Used for verification -- never triggers a real relaunch.
    [switch]$FleetResumeDryRun
)

$ErrorActionPreference = "SilentlyContinue"
# This script lives in <repo>\scripts. $Root is the REPO ROOT: .env, .venv and main.py
# (which this hosts) all live there.
$Root = Split-Path -Parent $PSScriptRoot

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

$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }
$Log = Join-Path $env:TEMP "m365-companion-supervisor.log"

# Prefer the winget-installed devtunnel (kept current) over an older copy that may
# be earlier on PATH (e.g. an IT-deployed one in System32). Older host builds drop
# their relay connection much more often. Falls back to "devtunnel" on PATH.
$DevTunnel = "devtunnel"
$wingetDt = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\devtunnel.exe"
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

function Write-Log($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg" | Out-File -FilePath $Log -Append -Encoding utf8
}

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
    try {
        $req = [System.Net.WebRequest]::Create("http://127.0.0.1:$Port/health")
        $req.Method = "GET"
        $req.Timeout = 5000
        $req.ReadWriteTimeout = 5000
        $resp = $req.GetResponse()
        $resp.Close()
        return $true
    } catch [System.Net.WebException] {
        $r = $_.Exception.Response
        if ($r) { $r.Close(); return $true }   # any HTTP status = app responded = alive
        return $false                           # timeout, refused, reset = down
    } catch { return $false }
}

function Test-TunnelHosting {
    $out = & $DevTunnel show $TunnelName 2>$null | Out-String
    if ($out -match 'Host connections\s*:\s*(\d+)') {
        return ([int]$Matches[1]) -ge 1
    }
    return $false
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

function Start-Server {
    # Kill the stale instance FIRST. A wedged main.py (dead event loop, CLOSE_WAIT
    # pile-up -- seen twice on 2026-06-12/13) keeps $Port LISTENING, so a new instance
    # can't bind and the supervisor restart-loops forever while the outage persists.
    # Scope: the process(es) owning $Port + any main.py launched from this repo's venv.
    try {
        Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique |
            ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
    } catch {}
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -match 'main\.py' -and $_.CommandLine -like "*$Root*"
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    Push-Location $Root
    Start-Process -FilePath $Py -ArgumentList "main.py" -WorkingDirectory $Root -WindowStyle Hidden
    Pop-Location
    Write-Log "MCP server (re)started (stale instances cleared)"
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
    Start-Process -FilePath $DevTunnel -ArgumentList "host $TunnelName" -WindowStyle Hidden
    Write-Log "devtunnel host starting for $TunnelName (bin=$DevTunnel) ..."
    # A freshly-started host can take 15-30s to register with the relay. Block until it
    # actually shows >=1 connection (up to ~50s) so the monitor loop never kills a host
    # that is still in the middle of connecting (which would cause a restart churn loop).
    for ($i = 0; $i -lt 25; $i++) {
        Start-Sleep -Seconds 2
        if (Test-TunnelHosting) {
            Write-Log "devtunnel host established (after ~$([int](($i + 1) * 2))s)"
            return
        }
    }
    Write-Log "devtunnel host did not establish within ~50s (will retry next cycle)"
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
$FleetMarkerPath = Join-Path $Root ".fleet\fleet_run_active.json"
$ReviewMarkerPath = Join-Path $Root ".fleet\review_run_active.json"
$script:LastReviewResumeKey = ""
$script:LastReviewResumeAttempt = [datetime]::MinValue

function Test-FleetAutoResumeEnabled {
    $v = $env:MCP_FLEET_AUTORESUME
    if ([string]::IsNullOrWhiteSpace($v)) { return $true }   # unset -> default ON
    return -not ($v -in @("0", "false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF"))
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
    param($Marker)
    if ($null -eq $Marker) { return $false }
    $procId = 0
    try { $procId = [int]$Marker.pid } catch { return $false }
    if ($procId -le 0) { return $false }
    return -not (Test-PidAlive -ProcId $procId)
}

function Invoke-FleetAutoResume {
    # Returns $true iff it (would have) relaunched the coordinator; $false otherwise.
    # -DryRun logs the would-be relaunch command without starting a process.
    param([switch]$DryRun)
    if (-not (Test-FleetAutoResumeEnabled)) {
        Write-Log "fleet auto-resume disabled via MCP_FLEET_AUTORESUME -- skipping check"
        return $false
    }
    $marker = Get-FleetActiveMarker
    if (-not (Test-FleetShouldAutoResume $marker)) {
        return $false
    }
    $resumeArgs = @()
    if ($marker.resume_argv) { $resumeArgs = @($marker.resume_argv) }
    $resumeArgs = @($resumeArgs) + "--resume"
    $shown = ($resumeArgs -join " ")
    Write-Log "fleet run INTERRUPTED (marker pid $($marker.pid) is dead) -> auto-resuming: python -m relay.fleet_runner $shown"
    if ($DryRun) {
        Write-Log "fleet auto-resume DRY RUN -- not relaunching (verification mode)"
        return $true
    }
    try {
        Start-Process -FilePath $Py -ArgumentList (@("-m", "relay.fleet_runner") + $resumeArgs) `
            -WorkingDirectory $Root -WindowStyle Hidden
        Write-Log "fleet coordinator relaunched with --resume"
        try {
            & $Py -c "import sys; sys.path.insert(0, r'$Root'); from tools.notify_ops import notify_desktop; notify_desktop('Fleet auto-resumed', 'An interrupted overnight fleet run was detected after startup and relaunched with --resume.')" 2>$null | Out-Null
        } catch { }
        return $true
    } catch {
        Write-Log "fleet auto-resume FAILED to relaunch: $($_.Exception.Message)"
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
    $key = "$($marker.started)|$($marker.stamp)|$($marker.restart_count)"
    $since = ((Get-Date) - $script:LastReviewResumeAttempt).TotalSeconds
    if ($key -eq $script:LastReviewResumeKey -and $since -lt 300) { return $false }
    $script:LastReviewResumeKey = $key
    $script:LastReviewResumeAttempt = Get-Date
    $shown = $resumeArgs -join " "
    Write-Log "review pipeline INTERRUPTED (marker pid $procId is dead) -> auto-resuming: python -m bench.review_run $shown"
    try {
        Start-Process -FilePath $Py -ArgumentList (@("-m", "bench.review_run") + $resumeArgs) `
            -WorkingDirectory $Root -WindowStyle Hidden
        Write-Log "review pipeline relaunched from durable stamp $($marker.stamp)"
        return $true
    } catch {
        Write-Log "review auto-resume FAILED to relaunch: $($_.Exception.Message)"
        return $false
    }
}

Write-Log "supervisor up (tunnel=$TunnelName port=$Port interval=${IntervalSeconds}s debounce=$FailuresBeforeAction)"

# Checked once, here, before the forever health-check loop starts.
Invoke-FleetAutoResume -DryRun:$FleetResumeDryRun | Out-Null
Invoke-ReviewAutoResume | Out-Null

$serverMiss = 0
$tunnelMiss = 0
$loggedIn = $null   # tri-state ($null unknown / $true / $false) -- log only on transition

while ($true) {
    # Live self-correction ("never again" / defense-in-depth): if .env's MCP_TUNNEL_NAME
    # changed since this supervisor started hosting $TunnelName -- e.g. heal_tunnel.ps1's
    # self-heal repointed .env to this account's own tunnel while this supervisor was
    # already hosting a stale/borrowed one from a copied .env -- stop the OLD host and
    # switch to the new name. This makes the running supervisor self-correct even if
    # nothing ever re-runs start_all.bat (which also detects and restarts this drift, but
    # only at the moment it is invoked). Bare-name compare (ignores the ".cluster" suffix)
    # so a URL-only .env rewrite of the SAME tunnel never causes a needless churn.
    $freshTn = Get-EnvTunnelName
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
    }

    # Clear phantom fleet runs whose coordinator process died without a supervisor restart
    # (Invoke-FleetAutoResume above only runs once at supervisor startup, so a mid-session
    # coordinator kill/crash would otherwise leave .fleet/status.json stuck showing
    # running=true forever). Best-effort, idempotent, never relaunches anything -- see
    # relay/fleet_reaper.py.
    try {
        $reapOut = & $Py -c "import sys; sys.path.insert(0, r'$Root'); from relay.fleet_reaper import reap_stale_run; import json; r = reap_stale_run(); print(json.dumps(r) if r else '')" 2>$null
        if ($reapOut) { Write-Log "reaped stale fleet run: $reapOut" }
    } catch { }
    Invoke-ReviewAutoResume | Out-Null

    if (Test-ServerUp) {
        $serverMiss = 0
    } else {
        $serverMiss++
        Write-Log "server check failed ($serverMiss/$FailuresBeforeAction)"
        if ($serverMiss -ge $FailuresBeforeAction) {
            Start-Server
            $serverMiss = 0
            Start-Sleep -Seconds 6
        }
    }

    if (-not (Test-DevtunnelLoggedIn)) {
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
        if (Test-TunnelHosting) {
            $tunnelMiss = 0
        } else {
            $tunnelMiss++
            Write-Log "tunnel host connections = 0 ($tunnelMiss/$FailuresBeforeAction)"
            if ($tunnelMiss -ge $FailuresBeforeAction) {
                Start-TunnelHost
                $tunnelMiss = 0
                Start-Sleep -Seconds 8
            }
        }
    }

    Start-Sleep -Seconds $IntervalSeconds
}
