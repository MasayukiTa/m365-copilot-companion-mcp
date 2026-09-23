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
    # How long a main.py of ours may be alive-but-not-listening before its failures start
    # counting. It is not a guess at startup time so much as a ceiling on how long we are
    # willing to wait for one: imports alone measured 10-25s, and the whole boot is longer
    # under load. Only ever applies while such a process is actually alive.
    [int]$StartupGraceSeconds = 180,
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

$Log = Join-Path $env:TEMP "m365-companion-supervisor.log"

# A SUPERVISOR THAT CANNOT RUN PYTHON MUST NOT KEEP TRYING. Falling back to bare `python` was
# meant as a safety net, but on a machine with no .venv it usually resolves to the App Execution
# Alias under WindowsApps -- which is not an interpreter; run with arguments it returns an error
# code. The loop would then fail identically twice a pass, every fifteen seconds, forever, while
# reporting itself as running. Saying it once and stopping is better than doing nothing loudly.
$Py = Join-Path $Root ".venv\Scripts\python.exe"
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
    $script:LastServerCheck = "unknown"
    try {
        $req = [System.Net.WebRequest]::Create("http://127.0.0.1:$Port/health")
        $req.Method = "GET"
        $req.Timeout = 5000
        $req.ReadWriteTimeout = 5000
        $resp = $req.GetResponse()
        $resp.Close()
        $script:LastServerCheck = "ok"
        return $true
    } catch [System.Net.WebException] {
        $r = $_.Exception.Response
        if ($r) {
            $r.Close()
            $script:LastServerCheck = "answered"
            return $true                        # any HTTP status = app responded = alive
        }
        $script:LastServerCheck = [string]$_.Exception.Status   # Timeout / ConnectFailure / ...
        return $false
    } catch {
        $script:LastServerCheck = "threw: " + $_.Exception.GetType().Name
        return $false
    }
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
        $breq = [System.Net.WebRequest]::Create("http://127.0.0.1:8765/status")
        $breq.Method = "GET"; $breq.Timeout = 5000; $breq.ReadWriteTimeout = 5000
        $bresp = $breq.GetResponse()
        $bbody = (New-Object System.IO.StreamReader($bresp.GetResponseStream())).ReadToEnd()
        $bresp.Close()
        $bridgeBusy = ($bbody -match '"turn_running"\s*:\s*true') -or ($bbody -match '"busy"\s*:\s*true')
        $busy = $fleetRunning -or $bridgeBusy
    } catch {
        $busy = $true          # unreadable is busy, deliberately
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
    $script:TunnelHostProc = Start-Process -FilePath $DevTunnel -ArgumentList "host $TunnelName" -WindowStyle Hidden -PassThru
    Write-Log "devtunnel host starting for $TunnelName (bin=$DevTunnel) ..."
    # A freshly-started host can take 15-30s to register with the relay. Block until it
    # actually shows >=1 connection (up to ~50s) so the monitor loop never kills a host
    # that is still in the middle of connecting (which would cause a restart churn loop).
    # RETURNS WHETHER IT ESTABLISHED: a re-host that does not is the third invalidation event.
    for ($i = 0; $i -lt 25; $i++) {
        Start-Sleep -Seconds 2
        if (Test-TunnelHosting) {
            Write-Log "devtunnel host established (after ~$([int](($i + 1) * 2))s)"
            return $true
        }
    }
    Write-Log "devtunnel host did not establish within ~50s (will retry next cycle)"
    return $false
}
$script:TunnelHostProc = $null

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

# TRACKS AUTO-RESUME RUNNERS THIS SUPERVISOR ITSELF LAUNCHED (relay.fleet_runner /
# bench.review_run below), so a LATER tick can read back their exit code instead of the
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
        # -PASSTHRU, SAME REASON AS Start-Server's. Without it the launch is fire-and-forget
        # and a runner that dies before argparse (docs/agent_contract.md's own example: a bad
        # path, the wrong interpreter, a failed import) leaves literally nothing behind --
        # not even a marker, since that is written from inside main() and this death happens
        # before main() runs. Registering it here is what lets Invoke-AutoResumeRunnerCheck
        # (see its header, above the marker-path variables) read the exit code back later.
        $fleetLaunchAt = Get-Date
        $fleetProc = Start-Process -FilePath $Py -ArgumentList (@("-m", "relay.fleet_runner") + $resumeArgs) `
            -WorkingDirectory $Root -WindowStyle Hidden -PassThru
        Register-AutoResumeRunner -Proc $fleetProc -LaunchTime $fleetLaunchAt -Kind "fleet" `
            -CommandLine ('"' + $Py + '" -m relay.fleet_runner ' + $shown)
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

# -- Queue delivery: the reaper + router pass, and the wait between ticks -----------------------
# The two steps the tick has always run back to back, now callable from two places: the full
# tick (unchanged position and order) and the express pass inside Wait-ForNextTick below.
function Invoke-FleetReap {
    # Clear phantom fleet runs whose coordinator process died without a supervisor restart
    # (Invoke-FleetAutoResume above only runs once at supervisor startup, so a mid-session
    # coordinator kill/crash would otherwise leave .fleet/status.json stuck showing
    # running=true forever). Best-effort, idempotent, never relaunches anything -- see
    # relay/fleet_reaper.py.
    try {
        $reapOut = & $Py -c "import sys; sys.path.insert(0, r'$Root'); from relay.fleet_reaper import reap_stale_run; import json; r = reap_stale_run(); print(json.dumps(r) if r else '')" 2>$null
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
$portQueried = $false
try {
    $portOwner = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
    $portQueried = $true
} catch {
    $portOwner = $null
}
if ($portQueried -and -not $portOwner) {
    Write-Log "nothing is listening on :$Port at startup -> launching the server now, without the debounce"
    Start-Server
}
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
    }

    # The reaper, then the queue drain -- see Invoke-FleetReap / Invoke-QueueDrain for why each
    # exists. The pending names are snapshotted BEFORE the pass and marked seen after it, so
    # Wait-ForNextTick below does not run an express pass for a job this pass was already shown.
    $shownToPass = Get-PendingJobNames -Dir $PendingDir
    Invoke-FleetReap
    Invoke-QueueDrain
    foreach ($n in $shownToPass) { [void]$script:ExpressSeen.Add($n) }

    Invoke-ReviewAutoResume | Out-Null

    # Report any tracked fleet/review auto-resume runner that has exited since the last tick.
    # Every tick, not just after a relaunch, because the runner that needs reporting may still
    # be alive on the tick that launched it and only exit (quickly, if it never got past
    # argparse) on the very next one.
    Invoke-AutoResumeRunnerCheck

    if (Test-ServerUp) {
        $serverMiss = 0
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
            $alive = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                       Where-Object { $_.CommandLine -match 'main\.py' -and
                                      $_.CommandLine -like "*$Root*" })
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
        if (Test-TunnelHosting) {
            $tunnelMiss = 0
        } else {
            Clear-DevtunnelLoginCache "tunnel host connections = 0"
            $tunnelMiss++
            Write-Log "tunnel host connections = 0 ($tunnelMiss/$FailuresBeforeAction)"
            if ($tunnelMiss -ge $FailuresBeforeAction) {
                # NEVER RE-HOST ON A CACHED ANSWER. The cache was just dropped, so this asks
                # the CLI live; only a clear "logged in" lets Start-TunnelHost touch devtunnel.
                if ($script:DevtunnelLoginAnswerWasCached -and -not (Test-DevtunnelLoggedInCached)) {
                    Write-Log "devtunnel NOT logged in on the live re-check before re-hosting -> tunnel management PAUSED, not re-hosting."
                    $loggedIn = $false
                } else {
                    if (-not (Start-TunnelHost)) {
                        Clear-DevtunnelLoginCache "the re-host did not establish"
                    }
                    Start-Sleep -Seconds 8
                }
                $tunnelMiss = 0
            }
        }
    }

    Wait-ForNextTick -Seconds $IntervalSeconds -Dir $PendingDir -ExpressPass {
        Invoke-FleetReap
        Invoke-QueueDrain -Tag "express"
    }
}
