# Remove the per-user M365 Companion autostart.
# Removes BOTH the Startup-folder shortcut (primary mechanism) and the
# opportunistic Task Scheduler task (bonus, may never have been registered).
# Safe to run even if neither exists (idempotent), and never errors out.

$startup = [Environment]::GetFolderPath('Startup')
$lnk     = Join-Path $startup 'M365 Companion.lnk'

if (Test-Path $lnk) {
    try {
        Remove-Item -Path $lnk -Force -ErrorAction Stop
        Write-Host "Removed Startup shortcut: $lnk"
    }
    catch {
        Write-Host "Could not remove Startup shortcut $lnk : $($_.Exception.Message)"
    }
} else {
    Write-Host "Startup shortcut was not present (nothing to do): $lnk"
}

$TaskName = 'M365CompanionAutostart'
try {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Host "Removed scheduled task $TaskName"
    } else {
        Write-Host "$TaskName was not registered (nothing to do)."
    }
}
catch {
    Write-Host "$TaskName scheduled task not removed (absent or blocked; nothing to do)."
}

exit 0
