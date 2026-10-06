# Remove the per-user M365 Companion autostart.
# Removes BOTH the Startup-folder shortcut (primary mechanism) and the
# opportunistic Task Scheduler task (bonus, may never have been registered).
# Safe to run even if neither exists (idempotent), and never errors out.

$repo = Split-Path $PSScriptRoot -Parent
. (Join-Path $PSScriptRoot "win\convenience_marker.ps1")

$lnk = Get-StartupLauncherPath

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

# RECORD THE ANSWER, OR THE NEXT START UNDOES THIS. start_all.ps1 re-creates logon autostart
# from .setup\convenience_provisioned, and this script used to leave "autostart=yes" in it --
# so the Startup shortcut and the task came back at the very next start_all (new-PC analysis
# D11). Written even when nothing was registered: "no" is what the person just said.
if (Set-ConvenienceDecision $repo "autostart" "no") {
    Write-Host "Recorded autostart=no in .setup\convenience_provisioned -- start_all will not re-create it."
    Write-Host "To turn it back on: scripts\register-supervisor.ps1"
} else {
    Write-Host "WARNING: could not record autostart=no in .setup\convenience_provisioned."
    Write-Host "start_all may re-create the autostart; edit that file and set autostart=no."
}

exit 0
