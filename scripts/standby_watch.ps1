# standby_watch.ps1 -- cold-standby liveness switch for main.py (design case A).
#
# WHAT IT DOES (one pass, stateless, does NOT stay resident):
#   1. Poll the PRIMARY /health (default http://127.0.0.1:8000/health) for up to
#      -UnhealthySec seconds (default 30). A pass counts as healthy only when the
#      body is main.py's own /health answer: JSON {"status":"ok","server_pid":<int>>0}
#      -- the SAME body rule as supervisor.ps1 Get-HealthServerPid and
#      measure_public_url_ready.ps1 Test-IsHealthyMainPyBody, so a relay sign-in page
#      that returns 200 does NOT read as healthy.
#   2. If the primary answered healthy at least once inside the window: do nothing and
#      exit 0 (the common case -- primary is fine).
#   3. If the primary never answered healthy for the whole window: start a SECONDARY
#      copy of this same main.py on :8002 (default) with MCP_PORT set, using the Python
#      given by -SecondaryPython, then wait up to -SecondaryReadySec for the secondary
#      /health to go healthy. Exit 0 if it came up, non-zero if it did not.
#   4. If a secondary is ALREADY healthy on :8002, do not start a second one (idempotent).
#
# WHY COLD STANDBY: this PC's CPU/memory are always tight (see the design note
# muse_path_redundancy). A hot second main.py would roughly double main.py's footprint
# and could push the primary over the edge, so the secondary is started only on primary
# failure and is torn down again once the primary is back. The cost is the secondary
# start time, which is exactly what the standby test measures.
#
# STATELESS: this script keeps no state between runs. It decides purely from what
# /health says right now. It is meant to be invoked once per tick by an existing
# supervisor, NOT to loop on its own.
#
# HOW TO WIRE IT INTO THE SUPERVISOR (proposal -- not yet added to supervisor.ps1):
#   The supervisor already polls primary health each tick. Add one call per tick, e.g.
#     powershell -NoProfile -ExecutionPolicy Bypass -File scripts\standby_watch.ps1 `
#       -SecondaryPython "$Root\.venv\Scripts\python.exe" -Once
#   Run it with -Once so each supervisor tick is one stateless pass (the default is
#   already one pass; -Once is kept for an explicit, self-documenting call site). Point
#   -SecondaryPython at .venv (3.10) or, to exercise the design's runtime-split idea,
#   at .venv312\Scripts\python.exe so the standby runs on a DIFFERENT runtime than the
#   primary. Do NOT register this as its own always-on service: the supervisor is the
#   one resident loop, and a second resident watcher would be the kind of extra always-on
#   process the design is trying to avoid on this machine.
#
# ROLLBACK: there is nothing to undo. Stop calling it from the supervisor and, if a
# secondary is running, stop that one process (by PID). No persistent state is written.
#
# ASCII / ENGLISH ONLY (cmd/console safe). No BOM.

[CmdletBinding()]
param(
    [string]$PrimaryUrl = "http://127.0.0.1:8000/health",
    [int]$PrimaryPort = 8000,
    [int]$SecondaryPort = 8002,
    [string]$SecondaryPython = "",
    [string]$MainPy = "",
    [int]$UnhealthySec = 30,
    [int]$PollIntervalSec = 2,
    [int]$SecondaryReadySec = 60,
    [switch]$Once,
    [switch]$Json
)

# --- PURE helpers (no network, no process I/O; unit-tested) ------------------------------------

function Test-IsHealthyMainPyBody {
    # PURE. $true only when $Body is main.py's /health answer: JSON with status == "ok"
    # AND an integer server_pid > 0. Same rule as supervisor.ps1 Get-HealthServerPid and
    # measure_public_url_ready.ps1 -- a 200 that is a relay sign-in page or any other body
    # returns $false, so it is NOT counted as a live main.py.
    param([string]$Body)
    if (-not $Body) { return $false }
    try { $j = $Body | ConvertFrom-Json -ErrorAction Stop } catch { return $false }
    if (-not $j -or $j.status -ne "ok") { return $false }
    $sp = 0
    try { $sp = [int]$j.server_pid } catch { return $false }
    return ($sp -gt 0)
}

function Get-LocalHealthUrl {
    # PURE. Build the loopback /health URL for a port. Used to derive the secondary's URL
    # from -SecondaryPort and, when -PrimaryUrl is left at its default, the primary's.
    param([int]$Port)
    return ("http://127.0.0.1:{0}/health" -f $Port)
}

function Resolve-PrimaryHealthUrl {
    # PURE. If the caller passed an explicit -PrimaryUrl, use it. Otherwise derive it from
    # -PrimaryPort. This lets a test point the primary probe at 18000 with just -PrimaryPort.
    param([string]$PrimaryUrl, [int]$PrimaryPort, [string]$DefaultUrl)
    if ($PrimaryUrl -and $PrimaryUrl -ne $DefaultUrl) { return $PrimaryUrl }
    if ($PrimaryPort -and $PrimaryPort -ne 8000) { return (Get-LocalHealthUrl -Port $PrimaryPort) }
    return $PrimaryUrl
}

function Format-StandbyResultLine {
    # PURE. One ELN-ready line from the outcome. $null for a figure that never became ready
    # is rendered as "n/a". -SwitchSec is the measured seconds from the decision-to-start to
    # the secondary's first healthy /health (the standby switch time the design cares about).
    param(
        [string]$Decision,
        [Nullable[double]]$SwitchSec,
        [int]$SecondaryPort
    )
    $sw = if ($null -eq $SwitchSec) { "n/a" } else { ('{0:0.0}s' -f $SwitchSec) }
    return ("standby_watch: decision={0} secondary_port={1} switch_sec={2}" -f $Decision, $SecondaryPort, $sw)
}

# --- IMPURE: real network / process (skipped when dot-sourced for tests) -----------------------

function Invoke-HealthOnce {
    # IMPURE. One GET of a /health URL. Returns the body string, or "" on any error
    # (connection refused, timeout, non-200). Never throws.
    param([string]$Url, [int]$TimeoutSec = 3)
    try {
        $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec -ErrorAction Stop
        if ($resp.StatusCode -ne 200) { return "" }
        return [string]$resp.Content
    } catch {
        return ""
    }
}

function Wait-ForHealthy {
    # IMPURE. Poll $Url until a healthy main.py body is seen or $DeadlineSec elapses.
    # Returns a double = seconds until first healthy, or $null if it never became healthy.
    param([string]$Url, [int]$DeadlineSec, [int]$IntervalSec)
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $DeadlineSec) {
        $body = Invoke-HealthOnce -Url $Url
        if (Test-IsHealthyMainPyBody -Body $body) { return [double]$sw.Elapsed.TotalSeconds }
        Start-Sleep -Seconds $IntervalSec
    }
    return $null
}

function Start-Secondary {
    # IMPURE. Launch a secondary copy of main.py on $Port using $Python, with MCP_PORT set.
    # Returns the started process object. The child inherits a copy of the environment with
    # MCP_PORT overridden; the parent's own MCP_PORT is left unchanged.
    param([string]$Python, [string]$MainPy, [int]$Port)
    $env:MCP_PORT = [string]$Port
    try {
        $p = Start-Process -FilePath $Python -ArgumentList @($MainPy) -PassThru -WindowStyle Hidden
    } finally {
        Remove-Item Env:MCP_PORT -ErrorAction SilentlyContinue
    }
    return $p
}

# --- MAIN (guarded so the Pester test can dot-source this file without running it) --------------

if ($MyInvocation.InvocationName -ne '.') {
    $DefaultPrimaryUrl = "http://127.0.0.1:8000/health"
    $primaryUrl = Resolve-PrimaryHealthUrl -PrimaryUrl $PrimaryUrl -PrimaryPort $PrimaryPort -DefaultUrl $DefaultPrimaryUrl
    $secondaryUrl = Get-LocalHealthUrl -Port $SecondaryPort

    # Resolve main.py relative to this script's repo root when not given.
    if (-not $MainPy) {
        $MainPy = Join-Path (Split-Path -Parent $PSScriptRoot) "main.py"
    }
    # Resolve a default secondary interpreter when not given: prefer .venv312, else .venv.
    if (-not $SecondaryPython) {
        $root = Split-Path -Parent $PSScriptRoot
        $cand312 = Join-Path $root ".venv312\Scripts\python.exe"
        $cand310 = Join-Path $root ".venv\Scripts\python.exe"
        if (Test-Path $cand312) { $SecondaryPython = $cand312 }
        elseif (Test-Path $cand310) { $SecondaryPython = $cand310 }
        else { $SecondaryPython = "python" }
    }

    # 1. Is the primary healthy within the unhealthy window?
    $primaryHealthy = Wait-ForHealthy -Url $primaryUrl -DeadlineSec $UnhealthySec -IntervalSec $PollIntervalSec
    if ($null -ne $primaryHealthy) {
        $line = Format-StandbyResultLine -Decision "primary_ok" -SwitchSec $null -SecondaryPort $SecondaryPort
        if ($Json) { [pscustomobject]@{ decision = "primary_ok"; secondary_port = $SecondaryPort; switch_sec = $null } | ConvertTo-Json -Compress }
        else { Write-Output $line }
        exit 0
    }

    # 2. Primary is down. Is a secondary already healthy on :8002? (idempotent)
    $secBody = Invoke-HealthOnce -Url $secondaryUrl
    if (Test-IsHealthyMainPyBody -Body $secBody) {
        $line = Format-StandbyResultLine -Decision "secondary_already_up" -SwitchSec $null -SecondaryPort $SecondaryPort
        if ($Json) { [pscustomobject]@{ decision = "secondary_already_up"; secondary_port = $SecondaryPort; switch_sec = $null } | ConvertTo-Json -Compress }
        else { Write-Output $line }
        exit 0
    }

    # 3. Start the secondary and time how long until it answers healthy.
    $null = Start-Secondary -Python $SecondaryPython -MainPy $MainPy -Port $SecondaryPort
    $switch = Wait-ForHealthy -Url $secondaryUrl -DeadlineSec $SecondaryReadySec -IntervalSec $PollIntervalSec
    if ($null -ne $switch) {
        $line = Format-StandbyResultLine -Decision "secondary_started" -SwitchSec $switch -SecondaryPort $SecondaryPort
        if ($Json) { [pscustomobject]@{ decision = "secondary_started"; secondary_port = $SecondaryPort; switch_sec = $switch } | ConvertTo-Json -Compress }
        else { Write-Output $line }
        exit 0
    }

    $line = Format-StandbyResultLine -Decision "secondary_failed" -SwitchSec $null -SecondaryPort $SecondaryPort
    if ($Json) { [pscustomobject]@{ decision = "secondary_failed"; secondary_port = $SecondaryPort; switch_sec = $null } | ConvertTo-Json -Compress }
    else { Write-Output $line }
    exit 2
}
