@echo off
REM ===========================================================================
REM  doctor.bat -- one-glance health check for the companion stack.
REM  Double-click any time to see a GREEN/RED checklist of every link with the
REM  exact fix for each red line. ASCII / ENGLISH ONLY.
REM ===========================================================================
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\doctor.ps1"
echo.
pause
