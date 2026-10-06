@echo off
REM ===========================================================================
REM  Configure the M365 Copilot agent URLs in .env -- opens ONLY a dialog window.
REM  Double-click this any time to set / change the agent URLs (main, fleet,
REM  research, analyst). Routes through a windowless VBS so no console appears --
REM  just the URL-entry dialog. ASCII / ENGLISH ONLY.
REM ===========================================================================
cd /d "%~dp0"

REM WINDOWS SCRIPT HOST CAN BE SWITCHED OFF (D18), and on a managed PC often is. wscript then shows
REM "Windows Script Host access is disabled" (or nothing) and the dialog never opens, so this file
REM did nothing at all. Enabled=0 under either hive means disabled; then run the same dialog
REM through PowerShell directly -- a console window appears behind it, which is the only cost.
set "WSH_OFF="
for %%K in ("HKLM\SOFTWARE\Microsoft\Windows Script Host\Settings" "HKCU\SOFTWARE\Microsoft\Windows Script Host\Settings") do (
    reg query %%K /v Enabled 2>nul | findstr /r /i /c:"Enabled  *REG_SZ  *0 *$" /c:"Enabled  *REG_DWORD  *0x0 *$" >nul && set "WSH_OFF=1"
)
if defined WSH_OFF goto :direct

wscript.exe "%~dp0scripts\configure_env_hidden.vbs"
exit /b %ERRORLEVEL%

:direct
echo Windows Script Host is disabled on this PC; opening the dialog through PowerShell instead.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\configure_env.ps1"
exit /b %ERRORLEVEL%
