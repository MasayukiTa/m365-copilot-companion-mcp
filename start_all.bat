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
setlocal EnableExtensions
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_all.ps1"
echo.
echo (You can close this window. The stack keeps running in the background.)
pause
