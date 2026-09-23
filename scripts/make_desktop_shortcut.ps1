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

# Point at the WINDOWLESS launcher (scripts\start_all_hidden.vbs, run via wscript) so a
# double-click shows NO cmd/console window -- there is nothing for a user to accidentally
# close. Fall back to start_all.bat (repo root) only if the .vbs is missing (older checkout).
$target = Join-Path $scriptDir "start_all_hidden.vbs"
if (-not (Test-Path $target)) { $target = Join-Path $repo "start_all.bat" }
if (-not (Test-Path $target)) {
    Write-Host "No launcher (start_all_hidden.vbs / start_all.bat) found -- nothing to link." -ForegroundColor Yellow
    return
}

try {
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($lnk)
    # A .vbs target must launch via wscript.exe (windowless): set TargetPath=wscript and pass the
    # .vbs as the argument, so a double-click never shows a console and never prompts "how do you
    # want to open this". A .bat fallback is launched directly.
    if ($target.ToLower().EndsWith(".vbs")) {
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
