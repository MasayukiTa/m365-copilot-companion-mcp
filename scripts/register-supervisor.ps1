# Install per-user logon autostart via a Startup-folder shortcut.
# No admin privileges required, and no Windows Task Scheduler dependency.
# Task Scheduler (Register-ScheduledTask / schtasks.exe) is BLOCKED by policy on
# locked-down corporate PCs -- even non-elevated, per-user AtLogOn registration
# returns Access Denied there. The per-user Startup folder
# (%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup) is writable with no
# admin rights and Windows runs every shortcut in it at logon, so it is the
# primary autostart mechanism. Task Scheduler registration is still attempted
# afterwards as an opportunistic bonus, but its failure never fails this script.
#
# WHICH TARGET (D18 / START-16, 2026-09-24): start_background_hidden.vbs only works while
# Windows Script Host is enabled -- wscript.exe does NOTHING (no window, no error) when it is
# disabled by policy, so a Startup shortcut (or Task Scheduler action) pointing at it is
# silently dead: it "launches" at every logon and nothing ever comes up. This reuses
# preflight_policy.ps1's Test-WshEnabled check (via -CheckWshOnly, the same entry point
# start_all.bat already calls on every run) to pick the target for BOTH the Startup shortcut
# and the opportunistic Task Scheduler action below:
#   WSH enabled:   wscript.exe  scripts\start_background_hidden.vbs
#   WSH disabled:  powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden
#                  -File scripts\start_all.ps1 -NoUi -NoSplash   (same args the .vbs passes)
# This script always OVERWRITES the shortcut and the scheduled task action, so a shortcut that
# went dead because WSH was disabled AFTER it was registered is fixed by simply re-running this
# script (or quickstart.bat, when the person answers yes to autostart again).

$repo = Split-Path $PSScriptRoot -Parent
$scriptDir = $PSScriptRoot
$vbs  = Join-Path $repo 'scripts\start_background_hidden.vbs'
$ps1  = Join-Path $repo 'scripts\start_all.ps1'
. (Join-Path $PSScriptRoot "win\convenience_marker.ps1")

# Test-WshEnabled lives in preflight_policy.ps1; reused here (via -CheckWshOnly, a subprocess
# call, same as start_all.bat's own check) rather than a second copy of the registry paths.
# PREFLIGHT_TEST_WSH_ENABLED is inherited by the child process, so tests that set it still work.
# Fails OPEN (assumes WSH is enabled) if the probe itself cannot run.
function Test-WshAvailable {
    param([string]$ScriptDir)
    $preflight = Join-Path $ScriptDir "preflight_policy.ps1"
    if (-not (Test-Path -LiteralPath $preflight)) { return $true }
    try {
        $lines = @(& powershell -NoProfile -ExecutionPolicy Bypass -File $preflight -CheckWshOnly 2>$null)
        foreach ($l in $lines) { if ([string]$l -eq "WSH-ENABLED=0") { return $false } }
        return $true
    } catch {
        return $true
    }
}

$wshOk = Test-WshAvailable $scriptDir
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$ps1Args = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -NoUi -NoSplash' -f $ps1

if (-not $wshOk) {
    if (-not (Test-Path $ps1)) {
        Write-Host "ERROR: scripts\start_all.ps1 not found at $ps1"
        exit 1
    }
    Write-Host "Windows Script Host is disabled -- registering autostart directly via PowerShell (scripts\start_all.ps1 -NoUi -NoSplash)."
} else {
    if (-not (Test-Path $vbs)) {
        Write-Host "ERROR: start_background_hidden.vbs not found at $vbs"
        exit 1
    }
    Write-Host "Registering autostart via the windowless launcher (scripts\start_background_hidden.vbs via wscript.exe)."
}

$lnk = Get-StartupLauncherPath

try {
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($lnk)
    if ($wshOk) {
        $sc.TargetPath = Join-Path $env:SystemRoot 'System32\wscript.exe'
        $sc.Arguments  = '"{0}"' -f $vbs
    } else {
        $sc.TargetPath = $psExe
        $sc.Arguments  = $ps1Args
    }
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

# RECORDED AS THE PERSON'S ANSWER. Running this by hand is asking for logon autostart, and
# start_all keeps what .setup\convenience_provisioned says: without this line a hand-registered
# autostart was never re-created if it went missing, and unregister-supervisor.ps1's "no"
# (which it now records) would be the only answer the file could hold.
if (-not (Set-ConvenienceDecision $repo "autostart" "yes")) {
    Write-Host "WARNING: could not record autostart=yes in .setup\convenience_provisioned."
}

Write-Host "Installed autostart shortcut: $lnk"
Write-Host "It launches at every logon (per-user, no admin)."
if ($wshOk) {
    Write-Host "Start the background stack now:  wscript.exe `"$vbs`""
    Write-Host "Start the full UI stack now:     wscript.exe `"$repo\scripts\start_all_hidden.vbs`""
} else {
    Write-Host "Start the background stack now:  powershell -NoProfile -ExecutionPolicy Bypass -File `"$ps1`" -NoUi -NoSplash"
    Write-Host "Start the full UI stack now:     powershell -NoProfile -ExecutionPolicy Bypass -File `"$ps1`""
}
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

    if ($wshOk) {
        $action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument ('"{0}"' -f $vbs) -WorkingDirectory $repo
    } else {
        $action = New-ScheduledTaskAction -Execute $psExe -Argument $ps1Args -WorkingDirectory $repo
    }
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
