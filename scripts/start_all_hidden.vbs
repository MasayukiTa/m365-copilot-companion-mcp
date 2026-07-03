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
' 0 = hidden window, False = do not wait (fire-and-forget; the stack detaches itself).
sh.Run "powershell -NoProfile -ExecutionPolicy Bypass -File """ & here & "start_all.ps1""", 0, False
