' Windowless launcher for configure_env.ps1 -- shows ONLY the URL-entry WinForms
' dialog (which the .ps1 creates), with no cmd/powershell console window.
Dim sh, here
here = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = here
sh.Run "powershell -NoProfile -ExecutionPolicy Bypass -File """ & here & "configure_env.ps1""", 0, False
