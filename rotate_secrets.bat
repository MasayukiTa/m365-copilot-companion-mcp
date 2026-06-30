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
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if exist "%VENV_PY%" (
    "%VENV_PY%" "scripts\rotate_secrets.py" %*
) else (
    echo .venv python not found; falling back to "python" on PATH.
    python "scripts\rotate_secrets.py" %*
)

echo.
echo Done. Review the next-steps above, then restart the server.
pause
