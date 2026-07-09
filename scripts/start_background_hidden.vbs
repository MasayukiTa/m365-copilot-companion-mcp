' ===========================================================================
'  M365 Companion - WINDOWLESS background logon launcher.
'  Starts the backend stack only: MCP server, tunnel, companion Edge, and bridge.
'  It intentionally suppresses both the startup splash and the WPF UI windows.
'  Manual/desktop launchers should continue to use start_all_hidden.vbs.
' ===========================================================================
Dim sh, here
here = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = here
' 0 = hidden window, False = do not wait.
sh.Run "powershell -NoProfile -ExecutionPolicy Bypass -File """ & here & "start_all.ps1"" -NoUi -NoSplash", 0, False
