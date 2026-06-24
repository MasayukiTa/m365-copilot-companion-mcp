# =============================================================================
#  make_desktop_shortcut.ps1 -- put a one-double-click launcher on the Desktop
#  so daily startup is "click the icon" instead of "find start_all.bat in the
#  repo". Re-runnable (overwrites). Called at the end of quickstart.bat, and you
#  can run it by hand any time. ASCII / ENGLISH ONLY.
# =============================================================================
$ErrorActionPreference = "Stop"
$repo = $PSScriptRoot
if (-not $repo) { $repo = Split-Path -Parent $MyInvocation.MyCommand.Path }

$target = Join-Path $repo "start_all.bat"
if (-not (Test-Path $target)) {
    Write-Host "start_all.bat not found next to this script -- nothing to link." -ForegroundColor Yellow
    return
}

$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "M365 Companion.lnk"
try {
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($lnk)
    $sc.TargetPath = $target
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
