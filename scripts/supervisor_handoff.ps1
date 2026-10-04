<#
.SYNOPSIS
  Replaces a running supervisor with a fresh one (started by supervisor.ps1's self-restart).

.DESCRIPTION
  supervisor.ps1 holds the machine-wide mutex Global\m365-copilot-companion-supervisor, so a new
  supervisor cannot start while the old one lives (it would exit at once). The old supervisor
  therefore starts THIS script, confirms it is running, and exits. This script waits for the old
  process to be gone, then starts the new supervisor with the same arguments and checks that it
  survived its first seconds; it retries, because the one thing that must not happen is a machine
  with no supervisor.

  The MCP server and the devtunnel host are separate processes and keep running throughout; the
  new supervisor adopts them (it launches the server only when nothing listens).

  If the old process does not exit within -WaitSeconds, nothing is started: the old supervisor is
  still the supervisor and a second one would only be refused by the mutex.

  Everything is logged to the supervisor's own log (-LogPath). ASCII only: powershell.exe reads
  scripts with the console code page on the machines this runs on.
#>
param(
    [Parameter(Mandatory = $true)][int]$OldPid,
    [string]$TunnelName = "",
    [int]$Port = 8000,
    [int]$IntervalSeconds = 15,
    [int]$FailuresBeforeAction = 4,
    [int]$StartupGraceSeconds = 180,
    [string]$LogPath = "",
    [int]$WaitSeconds = 90,
    [int]$Attempts = 5,
    [int]$SurviveSeconds = 6,
    [int]$RetryGapSeconds = 20,
    [switch]$FleetResumeDryRun,
    [switch]$FleetCycleResumeLive
)

$ErrorActionPreference = "SilentlyContinue"
if (-not $LogPath) { $LogPath = Join-Path $env:TEMP "m365-companion-supervisor.log" }
function Write-HandoffLog($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  [handoff] $msg" | Out-File -FilePath $LogPath -Append -Encoding utf8
}

$supervisor = Join-Path $PSScriptRoot "supervisor.ps1"
if (-not (Test-Path -LiteralPath $supervisor)) {
    Write-HandoffLog "supervisor.ps1 not found at $supervisor -- nothing started"
    exit 2
}

# 1. Wait for the old supervisor to be gone (it exits right after confirming this script is up).
$deadline = (Get-Date).AddSeconds($WaitSeconds)
while ((Get-Date) -lt $deadline) {
    if (-not (Get-Process -Id $OldPid -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 500
}
if (Get-Process -Id $OldPid -ErrorAction SilentlyContinue) {
    Write-HandoffLog "old supervisor (pid $OldPid) is still running after ${WaitSeconds}s -- it stays the supervisor; nothing started"
    exit 1
}

# 2. Start the new one; a supervisor that dies on startup is retried, not accepted.
$psExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$supArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"{0}"' -f $supervisor))
if ($TunnelName) { $supArgs += @("-TunnelName", ('"{0}"' -f $TunnelName)) }
$supArgs += @("-Port", $Port, "-IntervalSeconds", $IntervalSeconds,
              "-FailuresBeforeAction", $FailuresBeforeAction, "-StartupGraceSeconds", $StartupGraceSeconds)
if ($FleetResumeDryRun) { $supArgs += "-FleetResumeDryRun" }
if ($FleetCycleResumeLive) { $supArgs += "-FleetCycleResumeLive" }

for ($i = 1; $i -le $Attempts; $i++) {
    $p = $null
    try { $p = Start-Process -FilePath $psExe -ArgumentList $supArgs -WindowStyle Hidden -PassThru } catch { $p = $null }
    if ($p) {
        $null = $p.WaitForExit($SurviveSeconds * 1000)
        if (-not $p.HasExited) {
            Write-HandoffLog "new supervisor started (pid $($p.Id), attempt $i) -- handoff complete"
            exit 0
        }
        Write-HandoffLog "new supervisor exited at once (code $($p.ExitCode), attempt $i of $Attempts)"
    } else {
        Write-HandoffLog "could not start the new supervisor (attempt $i of $Attempts)"
    }
    if ($i -lt $Attempts) { Start-Sleep -Seconds $RetryGapSeconds }
}
Write-HandoffLog "NO SUPERVISOR IS RUNNING: $Attempts attempts failed. Run scripts\start_all.bat (or scripts\start_all.ps1 -NoUi) to start it."
exit 3
