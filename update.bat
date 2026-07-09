@echo off
REM ===========================================================================
REM  m365-copilot-companion-mcp - update ZIP installs from GitHub Releases
REM
REM  For users who downloaded M365-Companion-*.zip instead of git clone.
REM  Preserves .env, .venv, logs, and local runtime state.
REM ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "PYEXE=.venv\Scripts\python.exe"
if exist "%PYEXE%" goto run

where py >nul 2>nul
if not errorlevel 1 (
    set "PYEXE=py -3"
    goto run
)

where python >nul 2>nul
if not errorlevel 1 (
    set "PYEXE=python"
    goto run
)

echo Python was not found. Run quickstart.bat first, then try update.bat again.
pause
exit /b 1

:run
%PYEXE% "%~dp0scripts\update_from_release.py" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo Update did not complete. See the message above.
) else (
    echo Update complete.
)
pause
exit /b %RC%
