@echo off
REM ===========================================================================
REM  m365-copilot-companion-mcp - DAILY one-click startup (2nd run onward)
REM
REM  Double-click this. It brings up the whole stack in one go and is fully
REM  IDEMPOTENT: anything already running is left alone (the Dev Tunnel host is
REM  NEVER killed), so it is safe to double-click any time, even mid-session.
REM    1. supervisor.ps1            (MCP server + Dev Tunnel host)
REM    2. companion Edge :9222      (fleet / agent)
REM    3. bridge :9223 + chat       (start_bridge.ps1 -Keepalive, headless)
REM    4. CopilotChat + FleetCockpit windows
REM
REM  ASCII / ENGLISH ONLY (cmd corrupts non-ASCII). First-time setup is still
REM  quickstart.bat; this is the lightweight daily launcher.
REM ===========================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

REM WINDOWS SCRIPT HOST CAN BE DISABLED (D18 / START-16, 2026-09-24). When it is, wscript.exe
REM running the hidden .vbs below does NOTHING -- no window, no error, no non-zero exit code
REM (see scripts\preflight_policy.ps1's wsh-disabled finding) -- so checking wscript's own exit
REM code can never catch this case; the registry has to be asked directly, first. setup.bat's own
REM preflight already runs this same check at INSTALL time and only WARNs, so a policy flipped
REM off afterwards (or never checked because SETUP_IGNORE_POLICY was set) would otherwise make
REM every later double-click of this file silently do nothing, forever.
REM
REM scripts\win\wsh_vbs_check.ps1, NOT preflight_policy.ps1 (2026-09-25): this runs before the
REM single-instance lock and the leave/wait/run decision even happen, so ten double-clicks at
REM once means ten concurrent powershell.exe cold-starts here -- measured pushing an unrelated
REM LEAVING copy's own internal timing over its bound (scripts\test_start_all_ten_clicks.py)
REM purely from the CPU/IO cost of ten copies each parsing preflight_policy.ps1's full ~250
REM lines. wsh_vbs_check.ps1 holds the same two registry-check functions (preflight_policy.ps1
REM dot-sources it, so there is still exactly one copy of the registry paths) in a file a tenth
REM the size, with nothing else to parse.
set "WSH_OK=1"
for /f "usebackq delims=" %%W in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\win\wsh_vbs_check.ps1" -CheckWshOnly 2^>nul`) do (
    if "%%W"=="WSH-ENABLED=0" set "WSH_OK=0"
)
if "!WSH_OK!"=="0" goto :wscript_fallback

REM Launch the whole stack fully HIDDEN + DETACHED via the windowless VBS, then
REM exit immediately. No console lingers (the old `pause` window was the "blank
REM terminal" that, if closed mid-startup, left the stack half-up). start_all.ps1
REM is idempotent, so this is safe to run any time.
wscript.exe "%~dp0scripts\start_all_hidden.vbs"
REM Belt-and-braces (START-16): wscript.exe itself refusing to run at all -- missing/blocked
REM binary, NoDefaultCurrentDirectoryInExePath, some other policy the registry check above does
REM not name -- is a second, different way this silently does nothing. That errorlevel used to
REM be left unread.
if errorlevel 1 goto :wscript_fallback
goto :eof

:wscript_fallback
echo.
echo   Windows Script Host is disabled, or wscript.exe could not run the hidden launcher, so the
echo   usual windowless start could not fire. Starting the stack directly via PowerShell instead
echo   (still hidden and detached; ask IT to enable Windows Script Host to restore the normal
echo   wscript path, or see scripts\start_all.ps1 to run it yourself).
REM MCP_STARTALL_LAUNCH_PARENT_NAME (2026-09-25, scripts/test_start_all_ten_clicks.py): set on
REM THIS process before Start-Process, so the started start_all.ps1 inherits it and skips its own
REM WMI lookup for who its parent is -- see Get-LaunchLineage's own comment. No pid to hand down
REM here (Start-Process creates the actual parent process after this line runs), only the name,
REM which is fixed no matter which powershell.exe Start-Process ends up creating.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:MCP_STARTALL_LAUNCH_PARENT_NAME = 'powershell.exe'; Start-Process -FilePath 'powershell' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File','%~dp0scripts\start_all.ps1') -WindowStyle Hidden -WorkingDirectory '%~dp0'"
goto :eof
