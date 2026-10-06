' ===========================================================================
'  M365 Companion - WINDOWLESS daily launcher.
'  Runs start_all.ps1 fully HIDDEN and DETACHED: no console window ever appears,
'  so there is nothing for anyone to accidentally close mid-startup. start_all.ps1
'  is idempotent (already-running parts are left alone), so this is safe to run
'  any number of times. This .vbs is what the desktop shortcut points to.
' ===========================================================================
Dim sh, here
here = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = here
' MCP_STARTALL_LAUNCH_PARENT_NAME (2026-09-25, scripts/test_start_all_ten_clicks.py): tell
' start_all.ps1 who its parent is instead of making it ask Windows. wscript.exe has no built-in
' way to read its OWN process id (the one non-free thing here), but it does not need one -- this
' .vbs is ALWAYS run by wscript.exe, so the name is a known constant, not a lookup. Set on the
' PROCESS environment block so sh.Run's child (start_all.ps1) inherits it; see
' Get-LaunchLineage's own comment for why a leaving copy skips its WMI lookup when this is set.
sh.Environment("Process")("MCP_STARTALL_LAUNCH_PARENT_NAME") = "wscript.exe"
' 0 = hidden window, False = do not wait (fire-and-forget; the stack detaches itself).
sh.Run "powershell -NoProfile -ExecutionPolicy Bypass -File """ & here & "start_all.ps1""", 0, False
