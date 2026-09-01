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

REM --- Corporate TLS interception (ALL routes) -----------------------------------------
REM uv is a Rust binary carrying its OWN root certificates; it does not read the
REM Windows certificate store. Behind a TLS-intercepting proxy every uv download
REM then fails with "invalid peer certificate: UnknownIssuer" -- while PowerShell
REM on the same network succeeds, which is why uv installs and then cannot fetch
REM a Python. Export the roots this machine already trusts and point uv at them.
REM Verification stays ON: the corporate CA is a real trust anchor here, the only
REM problem was that some tools could not see it.
REM These are scoped by the setlocal above, so nothing leaks machine-wide.
REM DONE FOR EVERY ROUTE, not just the uv one. A machine that already has Python skips
REM the uv branch entirely, and anything bootstrap.py fetches over TLS would then run
REM without the bundle -- the same 'guard on one path only' shape this whole fix is about.
for /f "usebackq delims=" %%B in (`powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\ca_bundle.ps1" 2^>nul`) do set "CABUNDLE=%%B"
if defined CABUNDLE (
    echo Using this machine's trusted roots: !CABUNDLE!
    set "UV_NATIVE_TLS=1"
    if not defined SSL_CERT_FILE set "SSL_CERT_FILE=!CABUNDLE!"
    if not defined REQUESTS_CA_BUNDLE set "REQUESTS_CA_BUNDLE=!CABUNDLE!"
    if not defined CURL_CA_BUNDLE set "CURL_CA_BUNDLE=!CABUNDLE!"
    if not defined NODE_EXTRA_CA_CERTS set "NODE_EXTRA_CA_CERTS=!CABUNDLE!"
) else (
    echo Could not export the machine's root certificates; continuing with uv's own.
    set "UV_NATIVE_TLS=1"
)


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
    set "RC=1" & goto :done
)

:use_uv
REM uv provides a managed CPython without admin; create / reuse the venv.
echo Using uv at "!UVEXE!" to provision Python and .venv (no admin) ...
"!UVEXE!" python install 3.12
REM CHECK THE EXIT CODE. This was unchecked, so a failed download fell through to
REM "uv venv" -- which failed for the same reason -- and the operator was shown one
REM message about the second failure and none about the first.
if errorlevel 1 goto :uv_failed
if not exist ".venv\Scripts\python.exe" (
    "!UVEXE!" venv .venv
)
if exist ".venv\Scripts\python.exe" (
    set "PYEXE=.venv\Scripts\python.exe"
    goto :have_python
)

:uv_failed
echo.
echo ACTION NEEDED: 'uv' could not provision a Python.
echo.
echo   If the error above says "invalid peer certificate: UnknownIssuer", this
echo   network inspects TLS and uv cannot see this machine's root certificates.
echo   Setup already tried to export them automatically. If it still fails:
echo.
echo   Option A: run setup.bat again -- the export is refreshed each run.
echo   Option B: if your IT gave you the proxy's certificate, save it as
echo       .setup\ca-extra.pem  (a .cer file works too) and re-run setup.bat.
echo       It is appended to the exported bundle. Use this when the certificate
echo       is not installed in this machine's Windows store.
echo   Option C: install Python yourself (no admin), then re-run setup.bat:
echo       https://www.python.org/downloads/windows/
echo       python-3.12.x-amd64.exe /passive InstallAllUsers=0 PrependPath=1
echo.
echo   Running "!UVEXE!" venv .venv by hand will NOT help: it has to download a
echo   Python over the same connection and fails the same way.
echo.
set "RC=1" & goto :done

:have_python
REM Hand off to the real, resumable logic. %* forwards --status / --reset / --only.
echo Using Python interpreter: !PYEXE!
!PYEXE! scripts\bootstrap.py %*
set "RC=%ERRORLEVEL%"
goto :done

:done
REM When double-clicked standalone, hold the window open so the output is
REM readable (a 2nd run finishes in <1s, which otherwise flashes shut). When
REM CALLed from quickstart.bat (FROM_QUICKSTART=1), skip -- quickstart pauses.
if not defined FROM_QUICKSTART (
    echo.
    echo Bootstrap finished ^(exit code %RC%^). This window stays open so you can
    echo read the output above. Press any key to close.
    pause >nul
)
endlocal & exit /b %RC%
