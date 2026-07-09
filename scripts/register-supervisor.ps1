# Install per-user logon autostart via a Startup-folder shortcut.
# No admin privileges required, and no Windows Task Scheduler dependency.
# Task Scheduler (Register-ScheduledTask / schtasks.exe) is BLOCKED by policy on
# locked-down corporate PCs -- even non-elevated, per-user AtLogOn registration
# returns Access Denied there. The per-user Startup folder
# (%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup) is writable with no
# admin rights and Windows runs every shortcut in it at logon, so it is the
# primary autostart mechanism. Task Scheduler registration is still attempted
# afterwards as an opportunistic bonus, but its failure never fails this script.

$repo = Split-Path $PSScriptRoot -Parent
$vbs  = Join-Path $repo 'scripts\start_background_hidden.vbs'

if (-not (Test-Path $vbs)) {
    Write-Host "ERROR: start_background_hidden.vbs not found at $vbs"
    exit 1
}

$startup = [Environment]::GetFolderPath('Startup')
$lnk     = Join-Path $startup 'M365 Companion.lnk'

try {
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($lnk)
    $sc.TargetPath = Join-Path $env:SystemRoot 'System32\wscript.exe'
    $sc.Arguments  = '"{0}"' -f $vbs
    $sc.WorkingDirectory = $repo
    $sc.WindowStyle = 7
    $sc.Description = 'M365 Companion auto-start (server, tunnel, bridge; no UI)'
    $sc.Save()
}
catch {
    Write-Host "ERROR: could not create Startup shortcut: $($_.Exception.Message)"
    exit 1
}

if (-not (Test-Path $lnk)) {
    Write-Host "ERROR: shortcut creation reported success but $lnk does not exist"
    exit 1
}

Write-Host "Installed autostart shortcut: $lnk"
Write-Host "It launches at every logon (per-user, no admin)."
Write-Host "Start the background stack now:  wscript.exe `"$vbs`""
Write-Host "Start the full UI stack now:     wscript.exe `"$repo\scripts\start_all_hidden.vbs`""
Write-Host "Remove:  scripts\unregister-supervisor.ps1"

# Opportunistic secondary: Task Scheduler, only where corporate policy allows it.
# The Startup-folder shortcut above is already the source of truth, so ANY
# failure here (Access Denied is expected on locked-down PCs) is swallowed and
# must never change this script's exit code.
try {
    $TaskName = 'M365CompanionAutostart'

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    }

    $action  = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('"{0}"' -f $vbs) -WorkingDirectory $repo
    $trigger = New-ScheduledTaskTrigger -AtLogOn

    try {
        $trigger.Delay = 'PT15S'
    } catch {}

    $principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) `
                                             -LogonType Interactive `
                                             -RunLevel Limited
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                                              -DontStopIfGoingOnBatteries `
                                              -StartWhenAvailable `
                                              -MultipleInstances IgnoreNew `
                                              -ExecutionTimeLimit ([TimeSpan]::Zero)

    Register-ScheduledTask -TaskName $TaskName `
                           -Action $action `
                           -Trigger $trigger `
                           -Principal $principal `
                           -Settings $settings `
                           -ErrorAction Stop | Out-Null

    Write-Host "Bonus: Task Scheduler task $TaskName also registered."
}
catch {
    Write-Host "Task Scheduler unavailable (corporate policy); Startup-folder shortcut is the active mechanism."
}

exit 0
