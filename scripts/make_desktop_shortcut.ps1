# =============================================================================
#  make_desktop_shortcut.ps1 -- put a one-double-click launcher on the Desktop
#  so daily startup is "click the icon" instead of "find start_all.bat in the
#  repo". Re-runnable (overwrites). Called at the end of quickstart.bat, and you
#  can run it by hand any time. ASCII / ENGLISH ONLY.
# =============================================================================
#
#  -Remove deletes the Desktop launcher AND records shortcut=no, so start_all stops
#  re-creating it. Deleting the icon by hand is not an answer start_all can read: the
#  record still says yes, and a missing launcher with a "yes" on record is re-created.
#
#  WHICH TARGET (D18 / START-16, 2026-09-24): the windowless .vbs launcher only works when
#  Windows Script Host is enabled -- wscript.exe does NOTHING (no window, no error) when it is
#  disabled by policy, so a shortcut pointing at it is silently dead. This reuses
#  preflight_policy.ps1's Test-WshEnabled check (via -CheckWshOnly, the same entry point
#  start_all.bat already calls on every run) to pick the target:
#    WSH enabled:   wscript.exe  scripts\start_all_hidden.vbs           (no console flash)
#    WSH disabled:  powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden
#                   -File scripts\start_all.ps1                        (same args the .vbs passes)
#  This script always OVERWRITES the shortcut, and quickstart.bat calls it unconditionally
#  whenever the person answers yes -- so a shortcut that went dead because WSH was disabled
#  AFTER it was created is fixed by simply running quickstart.bat (or this script) again.
#  start_all.ps1's own Ensure-ConvenienceProvisioning only re-creates a MISSING shortcut on
#  every start (it does not check whether an existing one still works), so that automatic path
#  does not by itself repair a dead shortcut -- re-running this script is what does.
param([switch]$Remove)
$ErrorActionPreference = "Stop"
# This script lives in <repo>\scripts. $repo is the REPO ROOT (one level up); $scriptDir is
# the scripts dir where the windowless .vbs launcher now lives.
$scriptDir = $PSScriptRoot
if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$repo = Split-Path -Parent $scriptDir
. (Join-Path $scriptDir "win\convenience_marker.ps1")
$lnk = Get-DesktopLauncherPath

if ($Remove) {
    # THE OTHER HALF OF D11 (new-PC analysis). start_all re-creates a missing launcher whenever
    # .setup\convenience_provisioned says shortcut=yes, so the durable way to be rid of it is to
    # change that answer, which is what this does.
    if (Test-Path -LiteralPath $lnk) {
        try { Remove-Item -LiteralPath $lnk -Force; Write-Host ("Removed Desktop launcher: " + $lnk) }
        catch { Write-Host ("Could not remove " + $lnk + ": " + $_.Exception.Message) -ForegroundColor Yellow }
    } else {
        Write-Host ("Desktop launcher was not present: " + $lnk)
    }
    if (Set-ConvenienceDecision $repo "shortcut" "no") {
        Write-Host "Recorded shortcut=no -- start_all will not re-create it. Undo: scripts\make_desktop_shortcut.ps1"
    } else {
        Write-Host "WARNING: could not record shortcut=no in .setup\convenience_provisioned; start_all may re-create the launcher." -ForegroundColor Yellow
    }
    return
}

# Test-WshEnabled lives in preflight_policy.ps1; reused here (via -CheckWshOnly, a subprocess
# call, same as start_all.bat's own check) rather than a second copy of the registry paths.
# PREFLIGHT_TEST_WSH_ENABLED is inherited by the child process, so tests that set it still work.
# Fails OPEN (assumes WSH is enabled) if the probe itself cannot run, which is this script's
# prior behavior on any PC where that is somehow the case.
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
$vbsTarget = Join-Path $scriptDir "start_all_hidden.vbs"
$ps1Target = Join-Path $scriptDir "start_all.ps1"

# Point at the WINDOWLESS launcher (scripts\start_all_hidden.vbs, run via wscript) so a
# double-click shows NO cmd/console window -- there is nothing for a user to accidentally
# close. That only works while Windows Script Host is enabled; when it is disabled, link
# straight to PowerShell instead (same args start_all_hidden.vbs itself passes), and fall back
# to start_all.bat (repo root) only if the .vbs is missing (older checkout) and WSH still works.
if (-not $wshOk) {
    if (-not (Test-Path $ps1Target)) {
        Write-Host "No launcher (scripts\start_all.ps1) found -- nothing to link." -ForegroundColor Yellow
        return
    }
    Write-Host "Windows Script Host is disabled -- linking directly to PowerShell (scripts\start_all.ps1)." -ForegroundColor Yellow
    $target = $ps1Target
} elseif (Test-Path $vbsTarget) {
    Write-Host "Linking to the windowless launcher (scripts\start_all_hidden.vbs via wscript.exe)."
    $target = $vbsTarget
} else {
    $target = Join-Path $repo "start_all.bat"
    if (-not (Test-Path $target)) {
        Write-Host "No launcher (start_all_hidden.vbs / start_all.bat) found -- nothing to link." -ForegroundColor Yellow
        return
    }
    Write-Host "scripts\start_all_hidden.vbs not found -- linking to start_all.bat instead." -ForegroundColor Yellow
}

try {
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($lnk)
    # A .vbs target must launch via wscript.exe (windowless): set TargetPath=wscript and pass the
    # .vbs as the argument, so a double-click never shows a console and never prompts "how do you
    # want to open this". Without WSH, link straight to powershell.exe with -WindowStyle Hidden
    # (the closest a direct shortcut can get to windowless) and the same args the .vbs passes. A
    # .bat fallback is launched directly.
    if (-not $wshOk) {
        $sc.TargetPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
        $sc.Arguments  = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $ps1Target + '"'
    } elseif ($target.ToLower().EndsWith(".vbs")) {
        $sc.TargetPath = (Join-Path $env:SystemRoot "System32\wscript.exe")
        $sc.Arguments  = '"' + $target + '"'
    } else {
        $sc.TargetPath = $target
    }
    $sc.WorkingDirectory = $repo
    $sc.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
    $sc.Description = "Start the M365 Copilot companion (server + Dev Tunnel + Edge + chat/cockpit)"
    $sc.Save()
    Write-Host ("Desktop launcher created: " + $lnk) -ForegroundColor Green
    Write-Host "Daily startup is now: double-click 'M365 Companion' on your Desktop." -ForegroundColor Green
    # Asking for the launcher by hand is an answer too; start_all keeps what is recorded.
    if (-not (Set-ConvenienceDecision $repo "shortcut" "yes")) {
        Write-Host "(could not record shortcut=yes in .setup\convenience_provisioned)" -ForegroundColor Yellow
    }
    Write-Host "Remove it for good with: scripts\make_desktop_shortcut.ps1 -Remove" -ForegroundColor Green
} catch {
    Write-Host ("Could not create the Desktop shortcut: " + $_.Exception.Message) -ForegroundColor Yellow
    Write-Host "You can still start the stack with start_all.bat in the repo folder." -ForegroundColor Yellow
}
