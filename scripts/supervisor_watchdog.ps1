# supervisor_watchdog.ps1 -- out-of-process backstop for scripts/supervisor.ps1.
#
# WHY THIS EXISTS. scripts/supervisor.ps1 is the single point of failure for every automatic
# recovery on this machine: it restarts the MCP server, re-hosts the Dev Tunnel, drains the
# queue and resumes interrupted fleet/review/local-loop runs. When the supervisor process
# ITSELF dies, all of that stops and nothing brings it back -- measured 2026-11-11/12: the
# supervisor (pid 25644) ended 23:58:02 with 0xC00000FD (a native thread stack overflow
# faulting in ntdll.dll, under free RAM 625 MB / CPU 96 pct); nothing restarted it for ~37
# minutes and the public URL answered 502 until a person ran start_all by hand. supervisor.ps1
# cannot restart itself after a hard kill. This watchdog, in its OWN process, does.
#
# WHAT IT DOES (one pass with -Once; otherwise loops every -PollIntervalSec):
#   1. Read .fleet/supervisor_heartbeat.json, which the live supervisor overwrites every loop
#      iteration (pid + local_time + resources). Compute how old that beat is.
#   2. Ask whether a supervisor process is alive: is the heartbeat's pid a running process
#      whose name is powershell/pwsh (the supervisor runs under PowerShell).
#   3. Feed those two facts to the SAME decision table as scripts/supervisor_watchdog_gate.py
#      (pinned by test_supervisor_watchdog_gate.py on Linux and this script's own Pester test):
#      relaunch ONLY when the supervisor is NOT alive AND its heartbeat is stale (older than
#      -StaleThresholdSec, default 180s = 3 min) or absent; otherwise wait.
#   4. On 'relaunch', start scripts/start_all.ps1 -CoreOnly -NoSplash (hidden). It NEVER kills
#      anything -- not the supervisor, not a devtunnel host, not an interactive login. The only
#      action it can take is STARTING start_all when the supervisor is gone.
#
# SAFE DIRECTION: the danger is relaunching on top of a HEALTHY supervisor, so every ambiguous
# or unreadable input fails to 'wait' (see the gate module). A live supervisor always wins.
#
# -WhatIf decides and reports but starts nothing (used by the demo and by anyone checking it).
# -Once runs a single pass (used by the Pester/demo harness); the default is a resident loop,
# because unlike standby_watch this IS the one resident backstop -- there is no supervisor loop
# above it to call it each tick.
#
# ASCII / ENGLISH ONLY (cmd/console safe). No BOM.

[CmdletBinding()]
param(
    [string]$HeartbeatPath = "",
    [string]$StartAll = "",
    [int]$StaleThresholdSec = 180,
    [int]$PollIntervalSec = 60,
    [switch]$Once,
    [switch]$WhatIf,
    [switch]$Json
)

# --- PURE helpers (no network, no process, no clock passed IN; unit-tested) --------------------

function ConvertTo-WatchdogDecision {
    # PURE. The SAME table as scripts/supervisor_watchdog_gate.decide_watchdog_action.
    # Returns 'relaunch' only when the supervisor is NOT alive AND its heartbeat is stale or
    # absent; otherwise 'wait'. Every ambiguous or unreadable input fails to 'wait':
    #   SupervisorAlive $null (cannot tell) -> treated as alive -> wait.
    #   HeartbeatAgeSec $null (no/unparseable beat) -> stale, but only acts when not alive.
    #   a non-positive or non-numeric window -> wait.
    param($HeartbeatAgeSec, $SupervisorAlive, $StaleThresholdSec = 180)
    if ($null -eq $SupervisorAlive) { return "wait" }
    if ($SupervisorAlive) { return "wait" }
    $window = 0.0
    if (-not [double]::TryParse([string]$StaleThresholdSec, [ref]$window)) { return "wait" }
    if ($window -le 0) { return "wait" }
    if ($null -eq $HeartbeatAgeSec) { return "relaunch" }
    $age = 0.0
    if (-not [double]::TryParse([string]$HeartbeatAgeSec, [ref]$age)) { return "wait" }
    if ($age -lt $window) { return "wait" }
    return "relaunch"
}

function Get-HeartbeatPidAndAge {
    # PURE. Parse a heartbeat JSON body against a reference 'now' and return
    # @{ pid = <int|$null>; age_sec = <double|$null> }. The supervisor writes local_time as
    # 'yyyy-MM-dd HH:mm:ss' (LOCAL time) each beat; age is Now minus that. A body that is empty,
    # not JSON, or missing/!parseable local_time yields age_sec $null (treated as stale by the
    # decision table, but only ever acted on when the supervisor is also not alive). A missing
    # or non-integer pid yields pid $null.
    param([string]$Body, [datetime]$Now)
    $result = @{ pid = $null; age_sec = $null }
    if (-not $Body) { return $result }
    $j = $null
    try { $j = $Body | ConvertFrom-Json -ErrorAction Stop } catch { return $result }
    if ($null -eq $j) { return $result }
    try { if ($null -ne $j.pid) { $result.pid = [int]$j.pid } } catch { $result.pid = $null }
    $lt = [string]$j.local_time
    if ($lt) {
        $t = [datetime]::MinValue
        if ([datetime]::TryParseExact($lt, 'yyyy-MM-dd HH:mm:ss',
                [System.Globalization.CultureInfo]::InvariantCulture,
                [System.Globalization.DateTimeStyles]::None, [ref]$t)) {
            $result.age_sec = ($Now - $t).TotalSeconds
        }
    }
    return $result
}

function Format-WatchdogResultLine {
    # PURE. One ELN-ready line from the outcome. $null age renders as 'n/a'.
    param([string]$Decision, $AgeSec, $SupervisorPid, $SupervisorAlive)
    $ageTxt = if ($null -eq $AgeSec) { "n/a" } else { ('{0:0.0}s' -f [double]$AgeSec) }
    $pidTxt = if ($null -eq $SupervisorPid) { "none" } else { [string]$SupervisorPid }
    $aliveTxt = if ($null -eq $SupervisorAlive) { "unknown" } elseif ($SupervisorAlive) { "yes" } else { "no" }
    return ("supervisor_watchdog: decision={0} supervisor_pid={1} alive={2} heartbeat_age={3}" -f $Decision, $pidTxt, $aliveTxt, $ageTxt)
}

# --- IMPURE: real disk / process (skipped when dot-sourced for tests) -------------------------

function Get-DefaultHeartbeatPath {
    # IMPURE-ish. .fleet/supervisor_heartbeat.json under the repo root (this script's parent's
    # parent), the exact path supervisor.ps1 writes ($Root\.fleet\supervisor_heartbeat.json).
    $root = Split-Path -Parent $PSScriptRoot
    return (Join-Path $root ".fleet\supervisor_heartbeat.json")
}

function Get-DefaultStartAllPath {
    $root = Split-Path -Parent $PSScriptRoot
    return (Join-Path $root "scripts\start_all.ps1")
}

function Read-HeartbeatBody {
    # IMPURE. Read the heartbeat file as one string, or '' if it is absent/unreadable.
    param([string]$Path)
    try {
        if (-not (Test-Path -LiteralPath $Path)) { return "" }
        return [string](Get-Content -LiteralPath $Path -Raw -ErrorAction Stop)
    } catch { return "" }
}

function Test-SupervisorAlive {
    # IMPURE. Is $SupervisorPid a RUNNING PowerShell process? Returns $true / $false, or $null
    # when it cannot tell (no pid, or the process query itself failed) -- $null is treated as
    # alive by the decision table (fail safe: never relaunch on an unknown). A pid that exists
    # but is NOT powershell/pwsh is a reused pid, not the supervisor, so it reads as not alive.
    param($SupervisorPid)
    if ($null -eq $SupervisorPid) { return $null }
    $p = $null
    try { $p = Get-Process -Id ([int]$SupervisorPid) -ErrorAction SilentlyContinue } catch { return $null }
    if ($null -eq $p) { return $false }
    $name = ""
    try { $name = [string]$p.ProcessName } catch { $name = "" }
    if ($name -match '^(?i)(powershell|pwsh)$') { return $true }
    return $false
}

function Start-StartAllCoreOnly {
    # IMPURE. Start scripts/start_all.ps1 -CoreOnly -NoSplash, hidden, detached. Returns the
    # started process object. It STARTS; it never kills. This is the watchdog's only action.
    param([string]$StartAll)
    $psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
              '-File', $StartAll, '-CoreOnly', '-NoSplash')
    return (Start-Process -FilePath $psExe -ArgumentList $args -PassThru -WindowStyle Hidden)
}

function Invoke-WatchdogPass {
    # IMPURE orchestration of ONE pass: read beat -> derive facts -> decide -> (maybe) relaunch.
    # Returns a hashtable with decision/age/pid/alive/relaunched for the caller to log. With
    # -WhatIfMode it decides and reports but starts nothing.
    param([string]$HeartbeatPath, [string]$StartAll, [int]$StaleThresholdSec, [switch]$WhatIfMode)
    $body = Read-HeartbeatBody -Path $HeartbeatPath
    $parsed = Get-HeartbeatPidAndAge -Body $body -Now (Get-Date)
    $alive = Test-SupervisorAlive -SupervisorPid $parsed.pid
    $decision = ConvertTo-WatchdogDecision -HeartbeatAgeSec $parsed.age_sec -SupervisorAlive $alive -StaleThresholdSec $StaleThresholdSec
    $relaunched = $false
    if ($decision -eq "relaunch" -and -not $WhatIfMode) {
        try { $null = Start-StartAllCoreOnly -StartAll $StartAll; $relaunched = $true } catch { $relaunched = $false }
    }
    return @{
        decision   = $decision
        age_sec    = $parsed.age_sec
        pid        = $parsed.pid
        alive      = $alive
        relaunched = $relaunched
    }
}

# --- MAIN (guarded so the Pester test can dot-source this file without running it) -------------

if ($MyInvocation.InvocationName -ne '.') {
    if (-not $HeartbeatPath) { $HeartbeatPath = Get-DefaultHeartbeatPath }
    if (-not $StartAll) { $StartAll = Get-DefaultStartAllPath }

    do {
        $r = Invoke-WatchdogPass -HeartbeatPath $HeartbeatPath -StartAll $StartAll -StaleThresholdSec $StaleThresholdSec -WhatIfMode:$WhatIf
        $line = Format-WatchdogResultLine -Decision $r.decision -AgeSec $r.age_sec -SupervisorPid $r.pid -SupervisorAlive $r.alive
        if ($WhatIf -and $r.decision -eq "relaunch") { $line = $line + " (WhatIf: not started)" }
        elseif ($r.relaunched) { $line = $line + " (relaunched start_all -CoreOnly -NoSplash)" }
        if ($Json) {
            [pscustomobject]@{
                decision = $r.decision; supervisor_pid = $r.pid; alive = $r.alive
                heartbeat_age_sec = $r.age_sec; relaunched = $r.relaunched; whatif = [bool]$WhatIf
            } | ConvertTo-Json -Compress
        } else {
            Write-Output $line
        }
        if (-not $Once) { Start-Sleep -Seconds $PollIntervalSec }
    } while (-not $Once)

    exit 0
}
