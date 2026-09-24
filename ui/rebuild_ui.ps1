# rebuild_ui.ps1 -- ONE reliable "build both, run the latest" for the WPF cockpit + chat.
#
# Why this exists: build_cockpit.bat / build_and_run.bat each end with `start <exe>`, so running
# them in sequence let FleetCockpit launch first and its OpenMain() relaunch the STALE CopilotChat
# before CopilotChat was rebuilt -- you ended up running an old binary. Invoking those .bats from
# PowerShell also hung on the `start`. This script removes the race: kill BOTH, build BOTH (csc,
# fail-fast, straight to the real exe paths), then launch BOTH fresh. No .bat, no `start`.
#
#   .\rebuild_ui.ps1            # stop, rebuild both, relaunch both
#   .\rebuild_ui.ps1 -NoLaunch  # stop + rebuild only (don't relaunch)
param([switch]$NoLaunch)

$ErrorActionPreference = "Stop"
$ui  = $PSScriptRoot
$FW  = "C:\Windows\Microsoft.NET\Framework64\v4.0.30319"
$CSC = "$FW\csc.exe"
$WPF = "$FW\WPF"
if (-not (Test-Path $CSC)) { Write-Host "ERROR: csc.exe not found ($CSC)"; exit 1 }

# 1) Stop BOTH first. FleetCockpit auto-relaunches CopilotChat (OpenMain), so killing only one lets
#    the other respawn a stale copy and re-lock the exe -> the rebuild then fails silently.
$stopped = Get-Process FleetCockpit,CopilotChat -ErrorAction SilentlyContinue
if ($stopped) { $stopped | ForEach-Object { Write-Host ("stopping " + $_.ProcessName + " pid=" + $_.Id) }; $stopped | Stop-Process -Force }
Start-Sleep -Milliseconds 900

# 2) Build both straight to the real exe paths (now unlocked). Fail-fast: stop on the first error.
function Build($name, $sources) {
    $out = Join-Path $ui ($name + ".exe")
    $refs = @("/r:$WPF\PresentationFramework.dll","/r:$WPF\PresentationCore.dll","/r:$WPF\WindowsBase.dll",
              "/r:$FW\System.Xaml.dll","/r:$FW\System.Web.Extensions.dll")
    if ($name -eq "FleetCockpit") { $refs += "/r:$FW\System.Windows.Forms.dll" }
    # CopilotChat reads gzipped transcripts (System.IO.Compression.GZipStream) -- not in csc's
    # default reference set, must be added explicitly or the build fails with CS0234/CS0246.
    if ($name -eq "CopilotChat") { $refs += "/r:$FW\System.IO.Compression.dll" }
    $manifest = if (Test-Path (Join-Path $ui "app.manifest")) { @("/win32manifest:$ui\app.manifest") } else { @() }
    $args = @("/nologo","/target:winexe","/out:$out") + $manifest + $refs + ($sources | ForEach-Object { Join-Path $ui $_ })
    $log = & $CSC @args 2>&1
    if ($LASTEXITCODE -ne 0) { Write-Host ("BUILD FAILED: " + $name); $log | Select-Object -Last 15 | ForEach-Object { Write-Host $_ }; exit 1 }
    $f = Get-Item $out
    Write-Host ("BUILD OK: {0,-13} {1} bytes  {2}" -f $name, $f.Length, $f.LastWriteTime.ToString("HH:mm:ss"))
}
# FleetCommands.cs is in BOTH lists on purpose: it is the single writer of the fleet command
# channel and both binaries send commands. Leaving it out of one list is not a build error --
# that binary just fails to compile, or worse, a future split would let one of them fall back
# to its own writer again, which is the race this file exists to remove.
# WindowSelfTest.cs is in both for the same kind of reason: it is the --selftest both exes
# answer (ui/test_both_windows_can_be_constructed.py). ChatSend.cs is the chat's send path,
# kept free of WPF so ui/test_the_chat_window_sends_what_was_typed.py can compile and run it.
# BridgeClient.cs is the chat's only way to the bridge (authenticated POST + X-Bridge-Token),
# kept free of WPF so ui/test_the_bridge_client_sends_the_token.py can run it against a bridge.
Build "FleetCockpit" @("FleetCockpit.cs","SelfImproveDashboard.cs","Theme.cs","FleetCommands.cs","WindowSelfTest.cs","SubmittedTasks.cs","FleetConvIdentity.cs")
Build "CopilotChat"  @("CopilotChat.cs","Markdown.cs","Theme.cs","FleetCommands.cs","ChatSend.cs","WindowSelfTest.cs","BridgeClient.cs","FleetConvIdentity.cs")

# 3) Launch both fresh (cockpit first; it will not relaunch a stale chat because we launch the new one).
if (-not $NoLaunch) {
    # THROUGH explorer, NOT DIRECTLY -- see the header note. A cockpit started as a child
    # of an agent session inherits that session's redirected AppData view, so its settings
    # land in a package-private copy that no coordinator ever reads. explorer.exe runs in
    # the ordinary desktop context, so a process it opens sees the same files the
    # operator's desktop shortcut does.
    Start-Process explorer.exe -ArgumentList (Join-Path $ui "CopilotChat.exe")
    Start-Process explorer.exe -ArgumentList (Join-Path $ui "FleetCockpit.exe")
    Start-Sleep -Milliseconds 800
    Start-Sleep -Milliseconds 1200
    Get-Process FleetCockpit,CopilotChat -ErrorAction SilentlyContinue |
        ForEach-Object { Write-Host ("running " + $_.ProcessName + " pid=" + $_.Id + " start=" + $_.StartTime.ToString("HH:mm:ss")) }
}
Write-Host "rebuild_ui: done."
