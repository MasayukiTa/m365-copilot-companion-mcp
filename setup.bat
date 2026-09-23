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
REM
REM  Knobs (environment variables, all optional):
REM    SETUP_UNBLOCK=1        remove Mark-of-the-Web from this folder without asking
REM    SETUP_IGNORE_POLICY=1  skip the execution-policy / language-mode preflight
REM    SETUP_PREFER_UV=1      ignore Python on PATH; use uv's own Python 3.12
REM    UV_INSTALLER_URL=...   where to fetch uv's installer (a mirror on a closed network)
REM ===========================================================================

REM --- 0. The install path itself (D23) -------------------------------------------------
REM CHECKED BEFORE DELAYED EXPANSION IS SWITCHED ON, because that is what breaks: with it on,
REM every "!" in %~dp0 is eaten, so a folder named "a!b" silently becomes "ab" wherever this
REM file or quickstart.bat expands its own location, and every later path points nowhere. The
REM comparison below only works while "!" is still an ordinary character. A UNC path fails
REM `cd /d` outright (cmd cannot make one current). Both are refused with the fix named,
REM instead of failing somewhere downstream with a message about something else.
setlocal EnableExtensions DisableDelayedExpansion
set "SETUP_HERE=%~dp0"
set "RC=1"
if "%SETUP_HERE:~0,2%"=="\\" goto :bad_unc
if not "%SETUP_HERE:!=%"=="%SETUP_HERE%" goto :bad_bang
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "PYEXE="
REM The oldest Python the requirements install on (fastmcp, mcp, anyio and ddgs declare >=3.10;
REM see MIN_PYTHON in scripts\bootstrap.py -- test_install_path_python_version.py keeps the two
REM equal). Exit code 3 = runs but too old, anything else non-zero = does not run.
set "PY_MIN_CHECK=import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 3)"
set "PY_MIN_TEXT=3.10"

REM --- 0b. Can this PC run this project's scripts at all? (D18) --------------------------
REM Group-Policy execution policy, Mark-of-the-Web on files from a downloaded ZIP, Constrained
REM Language Mode and a disabled Windows Script Host each defeat a later step silently. The
REM preflight names whichever is present and the exact next step. It is run through
REM Invoke-Expression, NOT -File: a policy that blocks script files would block the check too.
REM See scripts\preflight_policy.ps1 for the exit codes branched on here.
if defined SETUP_IGNORE_POLICY goto :after_preflight
powershell -NoProfile -ExecutionPolicy Bypass -Command "$PreflightRoot = (Get-Location).Path; iex (Get-Content -Raw -LiteralPath 'scripts\preflight_policy.ps1')"
set "PF_RC=!ERRORLEVEL!"
if "!PF_RC!"=="3" goto :offer_unblock
if "!PF_RC!"=="4" goto :offer_unblock
if "!PF_RC!"=="0" goto :after_preflight
REM PowerShell itself missing from PATH (cmd's own "command not found" errorlevel, 9009 --
REM see scripts\test_a_failing_entry_point_says_why.py for why this is not localization-
REM dependent) is NOT a policy block: nothing ran, so nothing was judged. Reporting it as one
REM told an operator with no PowerShell at all ("this PC's policy stops this project's
REM scripts") to ask IT for a script-execution exemption that has nothing to do with their
REM actual problem, and stopped setup.bat before it ever reached the Python checks below --
REM which is where a PC with no PowerShell and no Python needs to land, since that is the
REM branch that names python.org / astral as the next step (D18 follow-up, 2026-09-24).
if "!PF_RC!"=="9009" (
    echo.
    echo NOTE: PowerShell could not be started ^(not found on PATH: "!PATH!"^).
    echo   This is not a policy block -- the check that looks for one needs PowerShell to run
    echo   at all, so it could not be performed and is being skipped. Continuing with the
    echo   Python checks below; if this PC also has no Python, that step names the next step.
    goto :after_preflight
)
echo.
echo ACTION NEEDED: this PC's policy stops this project's scripts ^(see above^).
echo   Nothing was changed. Follow the NEXT STEP above, then run setup.bat again.
set "RC=1" & goto :done

:offer_unblock
echo.
if defined SETUP_UNBLOCK goto :do_unblock
choice /C YN /N /M "   Remove the downloaded-from-the-internet mark from this folder's files now? [Y/N] "
if "!ERRORLEVEL!"=="1" goto :do_unblock
if "!PF_RC!"=="4" (
    echo   Not removed. Windows refuses these scripts while they carry the mark, so setup
    echo   cannot continue. Run setup.bat again and answer Y, or use the command above.
    set "RC=1" & goto :done
)
echo   Left as it is.
goto :after_preflight

:do_unblock
echo   Removing the mark from every file under this folder ...
powershell -NoProfile -Command "Get-ChildItem -LiteralPath . -Recurse -File -Force -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue"
REM CHECKED AGAIN, NOT ASSUMED: Unblock-File can be refused per file, and a policy block that
REM was not only about the mark would still be there.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$PreflightRoot = (Get-Location).Path; iex (Get-Content -Raw -LiteralPath 'scripts\preflight_policy.ps1')" >nul
set "PF_RC=!ERRORLEVEL!"
if "!PF_RC!"=="0" (
    echo   Done: this folder's scripts can now run.
    goto :after_preflight
)
if "!PF_RC!"=="3" (
    echo   Done; some files still carry the mark ^(they could not be changed^), but scripts run.
    goto :after_preflight
)
echo   The scripts are still refused after removing the mark. Run setup.bat again to see
echo   the reason, and follow the NEXT STEP it prints.
set "RC=1" & goto :done

:after_preflight

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
    REM Both spellings: newer uv deprecates UV_NATIVE_TLS in favour of UV_SYSTEM_CERTS, and
    REM older uv does not know the new one. Setting both keeps either version working.
    set "UV_NATIVE_TLS=1"
    set "UV_SYSTEM_CERTS=1"
    if not defined SSL_CERT_FILE set "SSL_CERT_FILE=!CABUNDLE!"
    if not defined REQUESTS_CA_BUNDLE set "REQUESTS_CA_BUNDLE=!CABUNDLE!"
    if not defined CURL_CA_BUNDLE set "CURL_CA_BUNDLE=!CABUNDLE!"
    if not defined NODE_EXTRA_CA_CERTS set "NODE_EXTRA_CA_CERTS=!CABUNDLE!"
) else (
    echo Could not export the machine's root certificates; continuing with uv's own.
    set "UV_NATIVE_TLS=1"
    set "UV_SYSTEM_CERTS=1"
)

REM --- Proxy (D10) -------------------------------------------------------------------------
REM uv, pip and git take a proxy only from HTTPS_PROXY; none reads the proxy Windows is set up
REM with. On an explicit-proxy network the browser and PowerShell worked and those three failed,
REM and the failure text below talked about certificates. Derive it from this PC's own settings
REM (Internet Options, a PAC script, netsh winhttp) unless one is already set. Scoped by
REM setlocal like the certificate variables above.
set "SETUP_PROXY="
if defined HTTPS_PROXY set "SETUP_PROXY=!HTTPS_PROXY!"
if not defined SETUP_PROXY for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\detect_proxy.ps1" 2^>nul`) do set "SETUP_PROXY=%%P"
if defined SETUP_PROXY (
    if not defined HTTPS_PROXY set "HTTPS_PROXY=!SETUP_PROXY!"
    if not defined HTTP_PROXY set "HTTP_PROXY=!SETUP_PROXY!"
    if not defined NO_PROXY set "NO_PROXY=localhost,127.0.0.1,::1"
    echo Using this PC's proxy for downloads: !SETUP_PROXY!
)


REM --- 1. Prefer the project venv if it already exists ---------------------
REM EXISTENCE IS NOT RUNNABILITY. bootstrap.py is the repair logic, and a repair tool that
REM can only be launched by the interpreter it is meant to repair is no repair at all. A .venv
REM copied from another machine (folder copy / OneDrive sync / ZIP restore) carries a
REM python.exe whose base Python has moved or is gone, so it exists on disk yet cannot execute
REM -- and launching bootstrap.py with it dies before any of that repair runs. So PROBE it the
REM same way we already probe py/python below, and only adopt it if it actually runs AND is new
REM enough (D8: a venv built on Python 3.9 runs fine and can never hold the requirements). If
REM not, fall through to the py/python/uv routes, which rebuild the venv; do NOT set PYEXE to
REM an interpreter that cannot do the job.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "!PY_MIN_CHECK!" >nul 2>nul
    set "VRC=!ERRORLEVEL!"
    if "!VRC!"=="0" (
        set "PYEXE=.venv\Scripts\python.exe"
        goto :have_python
    )
    if "!VRC!"=="3" (
        echo .venv runs a Python older than !PY_MIN_TEXT!; it will be rebuilt on a newer one.
    ) else (
        echo .venv\Scripts\python.exe exists but could not run; looking for another Python.
    )
)

REM --- 2. Existing per-user Python on PATH ---------------------------------
REM NEW ENOUGH, NOT MERELY PRESENT (D8). Any Python 3 used to win here over uv's pinned 3.12, so
REM a PC whose `py -3` was 3.9 built the venv on it and then looped on "pip install failed
REM (network or a wheel build)". Too old = say so and fall through to uv.
REM SETUP_PREFER_UV=1 skips this step: a PATH Python that is new enough but broken in some other
REM way (a corporate build missing ensurepip, say) can be bypassed without uninstalling it, and CI
REM uses it to exercise the uv route on a runner that has Python on PATH.
if "!SETUP_PREFER_UV!"=="1" goto :find_uv
REM `call` BEFORE EVERY PATH-FOUND INTERPRETER. A `python` that is a .bat/.cmd shim (pyenv-win
REM installs exactly that) run WITHOUT call hands control to the shim and never comes back:
REM setup.bat simply ended, rc 0, nothing printed. Found by the install-path tests with a .cmd
REM stub, 2026-09-24. `call` is harmless in front of a real .exe.
where py >nul 2>nul
if not errorlevel 1 (
    call py -3 -c "!PY_MIN_CHECK!" >nul 2>nul
    set "PRC=!ERRORLEVEL!"
    if "!PRC!"=="0" (
        set "PYEXE=py -3"
        goto :have_python
    )
    if "!PRC!"=="3" (
        set "OLDVER="
        for /f "delims=" %%V in ('py -3 -c "import sys; print(sys.version.split()[0])" 2^>nul') do set "OLDVER=%%V"
        echo The Python found by 'py -3' ^(!OLDVER!^) is older than !PY_MIN_TEXT!, which this project needs.
        echo Skipping it; setup will use uv's own Python 3.12 instead ^(no admin, nothing else changes^).
    )
)
where python >nul 2>nul
if not errorlevel 1 (
    call python -c "!PY_MIN_CHECK!" >nul 2>nul
    set "PRC=!ERRORLEVEL!"
    if "!PRC!"=="0" (
        set "PYEXE=python"
        goto :have_python
    )
    if "!PRC!"=="3" (
        set "OLDVER="
        for /f "delims=" %%V in ('python -c "import sys; print(sys.version.split()[0])" 2^>nul') do set "OLDVER=%%V"
        echo The 'python' on PATH ^(!OLDVER!^) is older than !PY_MIN_TEXT!, which this project needs.
        echo Skipping it; setup will use uv's own Python 3.12 instead ^(no admin, nothing else changes^).
    )
)

:find_uv
REM --- 3. uv (Astral) already installed under the user profile -------------
REM RUN, NOT MERELY FOUND (D20). An interrupted download can leave a truncated .setup\bin\uv.exe
REM that exists and cannot execute; the existence check adopted it and every later run failed
REM at `uv python install` with certificate advice. Each candidate is executed; a broken copy in
REM OUR folder is deleted so the download below replaces it, one elsewhere is only skipped.
set "UVEXE="
if exist ".setup\bin\uv.exe" (
    ".setup\bin\uv.exe" --version >nul 2>nul
    if errorlevel 1 (
        echo .setup\bin\uv.exe is present but does not run ^(an interrupted download?^); replacing it.
        del /f /q ".setup\bin\uv.exe" >nul 2>nul
    )
)
for %%U in ("%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe" "%USERPROFILE%\.local\bin\uv.exe" ".setup\bin\uv.exe") do (
    if not defined UVEXE if exist "%%~U" (
        "%%~U" --version >nul 2>nul
        if errorlevel 1 (
            echo Found "%%~U" but it does not run; ignoring it.
        ) else (
            set "UVEXE=%%~U"
        )
    )
)
if defined UVEXE goto :use_uv

REM --- 4. Download uv into a per-user dir (NO admin) -----------------------
echo.
echo No suitable Python interpreter was found on PATH.
echo Attempting a no-admin install of 'uv' (Astral) into .setup\bin ...
echo.
if not exist ".setup\bin" mkdir ".setup\bin"
if not defined UV_INSTALLER_URL set "UV_INSTALLER_URL=https://astral.sh/uv/install.ps1"
REM OFFICIAL HOST ONLY, UNLESS OPTED IN (INST-10, 2026-09-24). UV_INSTALLER_URL exists so a
REM closed network can point at its own mirror of Astral's installer, but that URL's response
REM runs with this user's privileges -- setting the variable to anywhere else was a way to run
REM arbitrary code by setting one environment variable. A mirror is still supported: opt in
REM explicitly with UV_INSTALLER_URL_ALLOW_UNTRUSTED=1 alongside it.
echo !UV_INSTALLER_URL!| findstr /b /i /c:"https://astral.sh/" >nul
if errorlevel 1 if not "!UV_INSTALLER_URL_ALLOW_UNTRUSTED!"=="1" (
    echo   NOTE: UV_INSTALLER_URL is set to a host other than astral.sh: !UV_INSTALLER_URL!
    echo   Ignoring it and using the official installer instead -- that URL's response would
    echo   otherwise run with your user privileges. If this is a trusted mirror on a closed
    echo   network, set UV_INSTALLER_URL_ALLOW_UNTRUSTED=1 in this window to use it anyway.
    set "UV_INSTALLER_URL=https://astral.sh/uv/install.ps1"
)
REM Astral publish a standalone uv.exe; download the INSTALLER SCRIPT to a file first (not piped
REM straight into Invoke-Expression -- a response that never lands on disk cannot be re-run if
REM the connection drops mid-stream, and cannot be looked at either), run that file, then verify
REM what it produced before anything below trusts it: an Authenticode signature check first (uv.exe
REM has shipped one -- Azure Artifact Signing -- since release 0.12.12 / 2026-09-09; measured on
REM this machine's own devtunnel.exe with the same cmdlet: Get-AuthenticodeSignature reports
REM Status=Valid, Subject="CN=Microsoft Corporation, ..." for both the WinGet and System32 copies,
REM which is the same trust model applied to uv.exe here). A build with NO signature at all (an
REM older release, or a mirror that stripped it) falls back to an INDEPENDENT download of the
REM matching release asset and a SHA-256 check against Astral's own published checksum registry
REM (astral-sh/versions) -- comparing against the asset we fetch ourselves, not the one the
REM installer already extracted, since the registry's checksum is for the .zip, not the .exe
REM inside it. Either way this says on-screen which path it took. The proxy line makes the
REM installer's own downloads use the proxy found above, with this Windows sign-in for a proxy
REM that asks for one.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "try {" ^
  "  if ($env:HTTPS_PROXY) { [System.Net.WebRequest]::DefaultWebProxy = New-Object System.Net.WebProxy($env:HTTPS_PROXY, $true) };" ^
  "  [System.Net.WebRequest]::DefaultWebProxy.Credentials = [System.Net.CredentialCache]::DefaultNetworkCredentials;" ^
  "  $installerFile = Join-Path '.setup\bin' 'uv-install.ps1';" ^
  "  Invoke-WebRequest -UseBasicParsing -Uri $env:UV_INSTALLER_URL -OutFile $installerFile;" ^
  "  $env:UV_INSTALL_DIR = (Resolve-Path '.setup\bin').Path;" ^
  "  $env:UV_NO_MODIFY_PATH = '1';" ^
  "  & $installerFile;" ^
  "  $exe = Join-Path '.setup\bin' 'uv.exe';" ^
  "  if (-not (Test-Path $exe)) { Write-Host 'uv.exe not found after the installer ran.'; exit 1 };" ^
  "  $sig = Get-AuthenticodeSignature -LiteralPath $exe;" ^
  "  if ($sig.Status -eq 'Valid') {" ^
  "    Write-Host ('uv.exe Authenticode signature: Valid (' + $sig.SignerCertificate.Subject + ').');" ^
  "  } elseif ($sig.Status -eq 'NotSigned') {" ^
  "    Write-Host 'uv.exe carries no Authenticode signature (an older build); verifying it against Astrals published SHA-256 checksum registry instead.';" ^
  "    $ok = $false;" ^
  "    try {" ^
  "      $ver = ((& $exe --version) -join ' ').Trim() -replace '^uv\s+', '' -replace '\s.*$', '';" ^
  "      $plat = 'x86_64-pc-windows-msvc';" ^
  "      $reg = Invoke-RestMethod -UseBasicParsing 'https://raw.githubusercontent.com/astral-sh/versions/main/v1/uv.ndjson';" ^
  "      $match = $null;" ^
  "      foreach ($line in ($reg -split [char]10)) { if (-not $line.Trim()) { continue }; try { $o = $line | ConvertFrom-Json } catch { continue }; if ($o.version -eq $ver) { $match = $o; break } };" ^
  "      $art = $null;" ^
  "      if ($match) { $art = $match.artifacts | Where-Object { $_.platform -eq $plat } | Select-Object -First 1 };" ^
  "      if ($art -and $art.sha256 -and $art.url) {" ^
  "        $zip = Join-Path '.setup\bin' 'uv-verify.zip';" ^
  "        Invoke-WebRequest -UseBasicParsing -Uri $art.url -OutFile $zip;" ^
  "        $actual = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash;" ^
  "        if ($actual -ieq $art.sha256) {" ^
  "          $exdir = Join-Path '.setup\bin' 'uv-verify-extract';" ^
  "          if (Test-Path $exdir) { Remove-Item -LiteralPath $exdir -Recurse -Force };" ^
  "          Expand-Archive -LiteralPath $zip -DestinationPath $exdir -Force;" ^
  "          $found = Get-ChildItem -LiteralPath $exdir -Filter 'uv.exe' -Recurse | Select-Object -First 1;" ^
  "          if ($found) { Copy-Item -LiteralPath $found.FullName -Destination $exe -Force; $ok = $true; Write-Host ('SHA-256 verified against Astrals published checksum for uv ' + $ver + '; using that independently-downloaded copy.') };" ^
  "          Remove-Item -LiteralPath $exdir -Recurse -Force -ErrorAction SilentlyContinue;" ^
  "        } else { Write-Host ('SHA-256 mismatch for uv ' + $ver + ': got ' + $actual + ', registry says ' + $art.sha256 + '.') };" ^
  "        Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue;" ^
  "      } else { Write-Host ('No published checksum found for uv ' + $ver + ' (' + $plat + ') in the registry.') };" ^
  "    } catch { Write-Host ('Checksum verification failed: ' + $_.Exception.Message) };" ^
  "    if (-not $ok) { Remove-Item -LiteralPath $exe -Force -ErrorAction SilentlyContinue; Write-Host 'Refusing this uv.exe: it is neither Authenticode-signed nor checksum-verified.'; exit 1 };" ^
  "  } else {" ^
  "    Remove-Item -LiteralPath $exe -Force -ErrorAction SilentlyContinue;" ^
  "    Write-Host ('uv.exe Authenticode signature is ' + $sig.Status + ' -- refusing it.');" ^
  "    exit 1;" ^
  "  }" ^
  "} catch { Write-Host ('uv download failed: ' + $_.Exception.Message); exit 1 }"
set "UVEXE="
if exist ".setup\bin\uv.exe" (
    ".setup\bin\uv.exe" --version >nul 2>nul
    if not errorlevel 1 set "UVEXE=.setup\bin\uv.exe"
)
if not defined UVEXE (
    echo.
    echo ACTION NEEDED: Could not auto-install 'uv', and no suitable Python was found.
    if defined SETUP_PROXY (
        echo   This PC uses a proxy ^(!SETUP_PROXY!^) and the download went through it. If the
        echo   error above mentions the proxy, 407, or a timeout, the proxy refused it: ask IT
        echo   to allow astral.sh and github.com for your account, then re-run setup.bat.
    )
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
REM PROBED, NOT MERELY PRESENT (D20). A .venv\Scripts\python.exe left by an interrupted
REM `uv venv`, or one on a too-old Python, used to be kept because it existed, and bootstrap was
REM then launched with that interpreter -- so the repair in bootstrap never got to run, every
REM run. An unusable one is removed and rebuilt here.
set "VENV_OK="
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "!PY_MIN_CHECK!" >nul 2>nul
    if not errorlevel 1 set "VENV_OK=1"
)
if not defined VENV_OK if exist ".venv" (
    echo Removing the unusable .venv so it can be built again ...
    rmdir /s /q ".venv" >nul 2>nul
)
if not defined VENV_OK if exist ".venv" (
    echo.
    echo ACTION NEEDED: the old .venv folder could not be removed ^(a file in it is in use^).
    echo   Close any window or program started from this folder ^(a running server, an editor,
    echo   a terminal^), delete the .venv folder by hand, then run setup.bat again.
    set "RC=1" & goto :done
)
if not defined VENV_OK (
    REM --seed installs pip into the venv. Without it uv creates a perfectly good venv
    REM with NO pip, and the bootstrap's health probe reads that as "broken" and tries to
    REM delete it -- which is how a fresh machine ended up being told to remove .venv by
    REM hand, for a venv that was working.
    REM --python 3.12 PINS the interpreter just installed. Without it uv picks the first Python
    REM it discovers, which can be the very too-old one skipped above (D8).
    "!UVEXE!" venv --seed .venv --python 3.12
)
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "!PY_MIN_CHECK!" >nul 2>nul
    if not errorlevel 1 (
        set "PYEXE=.venv\Scripts\python.exe"
        goto :have_python
    )
)

:uv_failed
echo.
echo ACTION NEEDED: 'uv' could not provision a Python.
echo.
if defined SETUP_PROXY (
    echo   This PC reaches the internet through a proxy: !SETUP_PROXY!
    echo   Setup passed it to uv. If the error above mentions the proxy, "407", "tunnel",
    echo   "connection refused" or a timeout, the proxy is what refused the download:
    echo     - ask IT to allow github.com and astral.sh through the proxy for your account;
    echo     - if the proxy needs a user name and password, set it in this window first:
    echo         set HTTPS_PROXY=http://USER:PASSWORD@proxy-host:port
    echo       and run setup.bat again from the same window.
    echo.
    echo   Only if the error above says "invalid peer certificate: UnknownIssuer" instead:
) else (
    echo   If the error above says "invalid peer certificate: UnknownIssuer", this
    echo   network inspects TLS and uv cannot see this machine's root certificates.
)
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
call !PYEXE! scripts\bootstrap.py %*
set "RC=%ERRORLEVEL%"
goto :done

:bad_unc
echo.
echo ACTION NEEDED: this folder is on a network path: "%SETUP_HERE%"
echo   Windows' command prompt cannot run setup from a \\server\share location.
echo   Copy the whole folder to a local drive, for example
echo       %USERPROFILE%\m365-copilot-companion
echo   and run setup.bat ^(or quickstart.bat^) from there.
goto :done

:bad_bang
echo.
echo ACTION NEEDED: the folder path contains a "!" character:
echo       "%SETUP_HERE%"
echo   The command prompt drops "!" from paths inside these setup scripts, so every file
echo   would be looked for in the wrong place. Rename the folder ^(or move it^) to a path
echo   without "!", for example %USERPROFILE%\m365-copilot-companion, and run it from there.
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
endlocal & endlocal & exit /b %RC%
