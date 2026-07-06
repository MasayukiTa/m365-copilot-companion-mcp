# Register a per-user AtLogOn task for M365 Companion automatic startup
# No admin privileges required; launches idempotent stack bring-up at each logon
# Task survives reboots via Task Scheduler (runs as Limited user, not elevated)

$repo = Split-Path $PSScriptRoot -Parent
$vbs  = Join-Path $repo 'scripts\start_all_hidden.vbs'

if (-not (Test-Path $vbs)) {
    Write-Host "ERROR: start_all_hidden.vbs not found at $vbs"
    exit 1
}

$TaskName = 'M365CompanionAutostart'

# Remove existing task if present (idempotent)
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

try {
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
                           -Settings $settings

    Write-Host "Registered $TaskName (per-user, AtLogOn, non-elevated)."
    Write-Host "Start now without logout:  schtasks /Run /TN $TaskName"
    Write-Host "Remove:  scripts\unregister-supervisor.ps1"
}
catch {
    Write-Host "Registration failed: $($_.Exception.Message)"
    Write-Host "Fallback via schtasks:"
    $argString = "wscript.exe `"$vbs`""
    schtasks /Create /SC ONLOGON /TN $TaskName /TR $argString /RL LIMITED /F
    exit 1
}
