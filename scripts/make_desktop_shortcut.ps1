# =============================================================================
#  make_desktop_shortcut.ps1 -- put a one-double-click launcher on the Desktop
#  so daily startup is "click the icon" instead of "find start_all.bat in the
#  repo". Re-runnable (overwrites). Called at the end of quickstart.bat, and you
#  can run it by hand any time. ASCII / ENGLISH ONLY.
# =============================================================================
$ErrorActionPreference = "Stop"
# This script lives in <repo>\scripts. $repo is the REPO ROOT (one level up); $scriptDir is
# the scripts dir where the windowless .vbs launcher now lives.
$scriptDir = $PSScriptRoot
if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$repo = Split-Path -Parent $scriptDir

# Point at the WINDOWLESS launcher (scripts\start_all_hidden.vbs, run via wscript) so a
# double-click shows NO cmd/console window -- there is nothing for a user to accidentally
# close. Fall back to start_all.bat (repo root) only if the .vbs is missing (older checkout).
$target = Join-Path $scriptDir "start_all_hidden.vbs"
if (-not (Test-Path $target)) { $target = Join-Path $repo "start_all.bat" }
if (-not (Test-Path $target)) {
    Write-Host "No launcher (start_all_hidden.vbs / start_all.bat) found -- nothing to link." -ForegroundColor Yellow
    return
}

$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "M365 Companion.lnk"
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
} catch {
    Write-Host ("Could not create the Desktop shortcut: " + $_.Exception.Message) -ForegroundColor Yellow
    Write-Host "You can still start the stack with start_all.bat in the repo folder." -ForegroundColor Yellow
}
