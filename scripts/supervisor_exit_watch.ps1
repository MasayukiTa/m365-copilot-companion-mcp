<#
.SYNOPSIS
  Waits for one process (the supervisor) to end and writes HOW it ended to the supervisor log.

.DESCRIPTION
  Started by supervisor.ps1 for itself, as a separate hidden process. A hard kill runs no
  `finally`, so the supervisor cannot say how it died; the heartbeat only says WHEN it was last
  seen. 2026-10-19 15:30 left a supervisor gone with no cause in the event logs, the supervisor
  log or any script (the PowerShell logs on this machine only reach back a few hours).

  What this adds is the exit code and the surroundings, from outside the dead process:
    code 1           TerminateProcess from Stop-Process / taskkill /F / Task Manager
    code 0           a clean exit (the supervisor's own 'leaving its loop' line is also there)
    0xC0000005 etc.  a crash
    0xC000013A       console close / logoff / shutdown
  plus how many powershell.exe and python.exe processes are alive right then: a single targeted
  kill leaves them, a mass kill of shells does not.

  It never writes the supervisor's own 'leaving its loop' record (that line means the supervisor
  itself left cleanly) and never throws. ASCII only: powershell.exe reads scripts with the
  console code page on the machines this runs on.
#>
param(
    [Parameter(Mandatory = $true)][int]$WatchPid,
    [string]$LogPath = ""
)

$ErrorActionPreference = "SilentlyContinue"
if (-not $LogPath) { $LogPath = Join-Path $env:TEMP "m365-companion-supervisor.log" }

$proc = $null
try { $proc = [System.Diagnostics.Process]::GetProcessById($WatchPid) } catch { }
if (-not $proc) { exit 0 }

# Hold the handle open from here on: the exit code can only be read from a handle taken while
# the process was alive.
$name = ""
$started = $null
try { $name = $proc.ProcessName; $started = $proc.StartTime } catch { }
try { [void]$proc.Handle } catch { }

$proc.WaitForExit()

$code = $null
$when = $null
try { $code = $proc.ExitCode } catch { }
try { $when = $proc.ExitTime } catch { }

function Get-AliveCount([string]$Image) {
    try { return @(Get-Process -Name $Image -ErrorAction SilentlyContinue).Count } catch { return -1 }
}

$codeTxt = "unknown"
if ($null -ne $code) { $codeTxt = ("{0} (0x{1:X8})" -f [int]$code, [int]$code) }
$whenTxt = "unknown"
if ($null -ne $when) { $whenTxt = $when.ToString("yyyy-MM-dd HH:mm:ss") }
$ps = Get-AliveCount "powershell"
$py = Get-AliveCount "python"
$line = ("{0}  supervisor pid {1} ended: exit code {2}, exit time {3}; powershell.exe alive now: {4}, python.exe alive now: {5} (written by supervisor_exit_watch.ps1)" -f
         (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $WatchPid, $codeTxt, $whenTxt, $ps, $py)
try { Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8 -ErrorAction Stop } catch { }
exit 0
