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
    [string]$TunnelName = "m365-copilot-companion",
    [int]$Port = 8000,
    [int]$IntervalSeconds = 10,
    # Consecutive failed checks required before acting. Debounce avoids killing a
    # healthy tunnel on a single transient "devtunnel show" glitch (false positive),
    # which would itself cause an outage.
    [int]$FailuresBeforeAction = 2
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
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(2000, $false) -and $client.Connected
        $client.Close()
        return $ok
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
    Push-Location $Root
    Start-Process -FilePath $Py -ArgumentList "main.py" -WorkingDirectory $Root -WindowStyle Hidden
    Pop-Location
    Write-Log "MCP server (re)started"
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
