@echo off
REM Print the 3 exact values to paste into Copilot Studio's MCP connector.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\copilot_studio_values.ps1"
echo.
pause
