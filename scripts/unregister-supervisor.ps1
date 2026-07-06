# Remove the per-user M365 Companion autostart task
# Safe to run even if the task does not exist (idempotent)

$TaskName = 'M365CompanionAutostart'

try {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed $TaskName"
    } else {
        Write-Host "$TaskName was not registered (nothing to do)."
    }
}
catch {
    Write-Host "$TaskName was not registered (nothing to do)."
}
