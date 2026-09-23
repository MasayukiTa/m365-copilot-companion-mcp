@echo off
REM ===========================================================================
REM m365-copilot-companion-mcp - SECRET ROTATION launcher (ASCII / English only).
REM
REM Rotates the secrets in .env (MCP_API_KEY and/or MCP_UNLOCK_PASSWORD).
REM   rotate_secrets.bat              rotate BOTH secrets (default)
REM   rotate_secrets.bat --api-key    rotate only MCP_API_KEY
REM   rotate_secrets.bat --unlock     rotate only MCP_UNLOCK_PASSWORD
REM   rotate_secrets.bat --no-print   do not echo new values to the console
REM
REM All arguments are forwarded to scripts/rotate_secrets.py.
REM ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if exist "%VENV_PY%" (
    set "PYEXE=%VENV_PY%"
) else (
    echo .venv python not found; falling back to "python" on PATH.
    set "PYEXE=python"
)

"%PYEXE%" "scripts\rotate_secrets.py" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    if "%RC%"=="9009" (
        echo No Python interpreter found ^(no .venv, and "python" is not on PATH^). Nothing was rotated.
        echo Run setup.bat or quickstart.bat to install the project's Python environment, then run
        echo rotate_secrets.bat again.
    ) else (
        echo rotate_secrets.py failed ^(exit %RC%^). Secrets were NOT rotated -- see the message above.
    )
) else (
    echo Done. Review the next-steps above, then restart the server.
)
pause
exit /b %RC%
