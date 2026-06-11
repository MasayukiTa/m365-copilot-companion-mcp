@echo off
REM ===========================================================================
REM  m365-copilot-companion-mcp - resumable environment bootstrap (entrypoint)
REM
REM  This batch file is intentionally THIN. Its only job is to make a Python
REM  interpreter available, then hand off to scripts\bootstrap.py which holds
REM  ALL of the real, resumable logic.
REM
REM  ASCII / ENGLISH ONLY. Do NOT add non-ASCII characters to this file -- cmd
REM  mis-decodes them and corrupts the script.
REM
REM  Usage:
REM    setup.bat              Run / resume the full bootstrap.
REM    setup.bat --status     Show which steps are done / pending (no changes).
REM    setup.bat --reset      Clear saved progress (no system changes).
REM    setup.bat --only NAME  Run a single step by name.
REM
REM  No admin rights are required. Where Python is missing we prefer 'uv'
REM  (Astral) downloaded into a per-user directory, with py/python fallbacks.
REM ===========================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "PYEXE="

REM --- 1. Prefer the project venv if it already exists ---------------------
if exist ".venv\Scripts\python.exe" (
    set "PYEXE=.venv\Scripts\python.exe"
    goto :have_python
)

REM --- 2. Existing per-user Python on PATH ---------------------------------
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "PYEXE=py -3"
        goto :have_python
    )
)
where python >nul 2>nul
if not errorlevel 1 (
    python -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "PYEXE=python"
        goto :have_python
    )
)

REM --- 3. uv (Astral) already installed under the user profile -------------
set "UVEXE=%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe"
if not exist "!UVEXE!" set "UVEXE=%USERPROFILE%\.local\bin\uv.exe"
if not exist "!UVEXE!" set "UVEXE=.setup\bin\uv.exe"
if exist "!UVEXE!" goto :use_uv

REM --- 4. Download uv into a per-user dir (NO admin) -----------------------
echo.
echo No Python interpreter was found on PATH.
echo Attempting a no-admin install of 'uv' (Astral) into .setup\bin ...
echo.
if not exist ".setup\bin" mkdir ".setup\bin"
set "UVEXE=.setup\bin\uv.exe"
REM Astral publish a standalone uv.exe; download it with PowerShell (no admin).
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "try {" ^
  "  $env:UV_INSTALL_DIR = (Resolve-Path '.setup\bin').Path;" ^
  "  $env:UV_NO_MODIFY_PATH = '1';" ^
  "  Invoke-RestMethod -UseBasicParsing https://astral.sh/uv/install.ps1 | Invoke-Expression;" ^
  "} catch { Write-Host ('uv download failed: ' + $_.Exception.Message); exit 1 }"
if exist ".setup\bin\uv.exe" set "UVEXE=.setup\bin\uv.exe"
if not exist "!UVEXE!" (
    echo.
    echo ACTION NEEDED: Could not auto-install 'uv', and no Python was found.
    echo   Option A ^(recommended, no admin^): install uv manually, then re-run setup.bat
    echo       powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 ^| iex"
    echo   Option B ^(per-user python.org, no admin^): download the installer from
    echo       https://www.python.org/downloads/windows/ and run it with:
    echo       python-3.12.x-amd64.exe /passive InstallAllUsers=0 PrependPath=1
    echo   Then re-run setup.bat to continue from where it stopped.
    echo.
    exit /b 1
)

:use_uv
REM uv provides a managed CPython without admin; create / reuse the venv.
echo Using uv at "!UVEXE!" to provision Python and .venv (no admin) ...
"!UVEXE!" python install 3.12
if not exist ".venv\Scripts\python.exe" (
    "!UVEXE!" venv .venv
)
if exist ".venv\Scripts\python.exe" (
    set "PYEXE=.venv\Scripts\python.exe"
    goto :have_python
)
echo.
echo ACTION NEEDED: 'uv' was installed but creating .venv failed.
echo   Run manually, then re-run setup.bat:
echo       "!UVEXE!" venv .venv
echo.
exit /b 1

:have_python
REM Hand off to the real, resumable logic. %* forwards --status / --reset / --only.
echo Using Python interpreter: !PYEXE!
!PYEXE! scripts\bootstrap.py %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
