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
    # IMPORTANT: this default must match the real tunnel id (`devtunnel list`).
    # 2026-06-12: a relaunch without -TunnelName used a stale default and KILLED the
    # live tunnel host while "fixing" a tunnel that didn't exist. Keep this current.
    [string]$TunnelName = "companion-mcp",
    [int]$Port = 8000,
    [int]$IntervalSeconds = 15,
    # Consecutive failed checks required before acting. Debounce avoids killing a
    # healthy tunnel on a single transient "devtunnel show" glitch (false positive),
    # which would itself cause an outage. Raised 2 -> 4 (2026-06-13): tool bodies now
    # run off the event loop, so the loop should never stall, but a higher debounce is
    # cheap insurance against a single slow /health response triggering a needless kill
    # that would tear down a live Copilot MCP session.
    [int]$FailuresBeforeAction = 4
)

$ErrorActionPreference = "SilentlyContinue"
$Root = $PSScriptRoot
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
    Get-Process devtunnel -ErrorAction SilentlyContinue | Stop-Process -Force
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

Write-Log "supervisor up (tunnel=$TunnelName port=$Port interval=${IntervalSeconds}s debounce=$FailuresBeforeAction)"

$serverMiss = 0
$tunnelMiss = 0

while ($true) {
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

    Start-Sleep -Seconds $IntervalSeconds
}
