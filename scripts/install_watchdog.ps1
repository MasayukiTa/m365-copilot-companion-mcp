# install_watchdog.ps1 -- per-user logon autostart for the supervisor WATCHDOG
# (scripts/supervisor_watchdog.ps1).
#
# Mirrors register-supervisor.ps1 exactly: a Startup-folder shortcut (no admin, no Task
# Scheduler dependency -- both are BLOCKED by policy on locked-down corporate PCs), with Task
# Scheduler registration attempted afterwards only as an opportunistic bonus whose failure
# never fails this script.
#
# The watchdog is the backstop for supervisor.ps1, which is itself the backstop for the server
# and tunnel. supervisor.ps1 cannot restart ITSELF after a hard kill (0xC00000FD, measured
# 2026-11-11); this watchdog, in its own process, does. It only ever STARTS start_all.ps1
# -CoreOnly -NoSplash when the supervisor is gone -- it kills nothing.
#
# SEPARATE FROM THE MAIN AUTOSTART. register-supervisor.ps1 installs 'M365 Companion.lnk'
# (the whole stack). This installs a DIFFERENT shortcut, 'M365 Companion Watchdog.lnk', so the
# two coexist and removing one never removes the other. The Startup folder is resolved the same
# way convenience_marker.ps1 does -- honouring $env:M365_COMPANION_STARTUP_DIR so this script
# can be exercised by a test against a temp folder instead of the owner's real Startup folder.
#
# ASCII / ENGLISH ONLY. No BOM.

$repo = Split-Path $PSScriptRoot -Parent
$watchdog = Join-Path $repo 'scripts\supervisor_watchdog.ps1'
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

if (-not (Test-Path $watchdog)) {
    Write-Host "ERROR: scripts\supervisor_watchdog.ps1 not found at $watchdog"
    exit 1
}

# Hidden, non-elevated, runs the watchdog loop. Same -WindowStyle Hidden / -NoProfile /
# -ExecutionPolicy Bypass shape register-supervisor.ps1 uses for the direct-PowerShell target.
$wdArgs = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $watchdog

# Same Startup-folder resolution as convenience_marker.ps1 Get-StartupLauncherPath: the test
# override first, then the shell's known Startup folder. A different .lnk name than the main
# autostart so the two never collide.
$startupDir = $env:M365_COMPANION_STARTUP_DIR
if (-not $startupDir) { $startupDir = [Environment]::GetFolderPath('Startup') }
$lnk = Join-Path $startupDir 'M365 Companion Watchdog.lnk'

try {
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($lnk)
    $sc.TargetPath = $psExe
    $sc.Arguments  = $wdArgs
    $sc.WorkingDirectory = $repo
    $sc.WindowStyle = 7
    $sc.Description = 'M365 Companion supervisor watchdog (restarts the supervisor if it dies)'
    $sc.Save()
} catch {
    Write-Host "ERROR: could not create Startup shortcut: $($_.Exception.Message)"
    exit 1
}

if (-not (Test-Path $lnk)) {
    Write-Host "ERROR: shortcut creation reported success but $lnk does not exist"
    exit 1
}

Write-Host "Installed watchdog autostart shortcut: $lnk"
Write-Host "It launches at every logon (per-user, no admin) and relaunches the supervisor if it dies."
Write-Host "Start the watchdog now:  powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$watchdog`""

# Opportunistic secondary: Task Scheduler, only where corporate policy allows it. The Startup
# shortcut above is the source of truth, so ANY failure here (Access Denied is expected on
# locked-down PCs) is swallowed and never changes this script's exit code.
try {
    $TaskName = 'M365CompanionWatchdog'
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    }
    $action = New-ScheduledTaskAction -Execute $psExe -Argument $wdArgs -WorkingDirectory $repo
    $thisUser = "{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $thisUser
    try { $trigger.Delay = 'PT30S' } catch { }
    $principal = New-ScheduledTaskPrincipal -UserId $thisUser -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -ErrorAction Stop | Out-Null
    Write-Host "Bonus: Task Scheduler task $TaskName also registered."
} catch {
    Write-Host "Task Scheduler unavailable (corporate policy); Startup-folder shortcut is the active mechanism."
}

exit 0
