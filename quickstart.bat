@echo off
REM ===========================================================================
REM  m365-copilot-companion-mcp - ONE-CLICK quickstart
REM
REM  Double-click this file. It runs the full resumable bootstrap (Python +
REM  venv + requirements + a .env with fresh secrets), prints your Bearer token
REM  and unlock password so you can copy-paste them, optionally pulls updates
REM  from git, then starts the MCP server. Safe to re-run any time.
REM
REM  ASCII / ENGLISH ONLY (cmd corrupts non-ASCII). Never pushes to git.
REM ===========================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ===========================================================================
echo  STEP 1/7  Install Python, venv and requirements (resumable bootstrap)
echo ===========================================================================
REM Tell setup.bat it is being CALLED (not double-clicked), so it does not add
REM its own pause -- quickstart has its own pauses and a final one.
set "FROM_QUICKSTART=1"
call setup.bat
set "FROM_QUICKSTART="
if errorlevel 1 (
    echo.
    echo Bootstrap failed. Fix the error above and run quickstart.bat again.
    pause
    exit /b 1
)

REM --- Pick the interpreter the bootstrap produced --------------------------
set "PYEXE=.venv\Scripts\python.exe"
if not exist "%PYEXE%" (
    where py >nul 2>nul && set "PYEXE=py -3"
)
if not exist ".venv\Scripts\python.exe" (
    where python >nul 2>nul && set "PYEXE=python"
)

echo.
echo ===========================================================================
echo  STEP 2/7  Your secrets - copy these into your MCP client
echo ===========================================================================
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if /i "%%A"=="MCP_API_KEY" echo   Bearer token  (MCP_API_KEY)        : %%B
        if /i "%%A"=="MCP_UNLOCK_PASSWORD" echo   Unlock password ^(MCP_UNLOCK_PASSWORD^): %%B
    )
    echo.
    echo   The Bearer token authorizes read-only tools. The unlock password is
    echo   passed to unlock^(password^) to enable mutating/execution tools per IP.
    echo   Keep both secret. They live in .env ^(gitignored^).
) else (
    echo   .env not found - bootstrap may not have completed. Re-run quickstart.bat.
)

echo.
echo ===========================================================================
echo  STEP 3/7  Check git for updates (fetch only - never pushes)
echo ===========================================================================
git rev-parse --is-inside-work-tree >nul 2>nul
if errorlevel 1 (
    echo   Not a git checkout - skipping update check.
) else (
    echo   Fetching...
    git fetch --quiet
    set "BEHIND=0"
    for /f %%C in ('git rev-list --count HEAD..@{u} 2^>nul') do set "BEHIND=%%C"
    if "!BEHIND!"=="0" (
        echo   Up to date.
    ) else (
        echo   !BEHIND! update^(s^) available on the remote branch.
        set /p ANS="   Pull them now with a fast-forward? [y/N] "
        if /i "!ANS!"=="y" (
            git pull --ff-only
        ) else (
            echo   Skipped. You can pull later with: git pull --ff-only
        )
    )
)

echo.
echo ===========================================================================
echo  STEP 4/7  Dev Tunnel  (install + sign-in + tunnel + public URL)
echo ===========================================================================
echo   Installs the devtunnel CLI (winget or direct download), signs you in
echo   (browser or device code), creates the tunnel, and prints the PUBLIC URL.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_devtunnel.ps1"

echo.
echo ===========================================================================
echo  STEP 5/7  Copilot Studio  (the ONLY manual, by-hand step)
echo ===========================================================================
echo   Add an MCP connector in Copilot Studio, then create your companion agent.
echo   The 3 EXACT values to paste are printed below (full guide: README STEP 4):
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0copilot_studio_values.ps1"
echo   After creating the agent, open it in M365 Copilot and copy its address-bar
echo   URL -- you will paste it into a dialog in the next step.
echo.
echo   Press any key AFTER you have created the agent in Copilot Studio...
pause >nul

echo.
echo ===========================================================================
echo  STEP 6/7  Paste your agent URLs  (a dialog window opens)
echo ===========================================================================
echo   Paste the agent URL(s) into the dialog and click Save. Leave blank any
echo   you do not have yet -- you can re-run configure_env.bat later to add them.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0configure_env.ps1"

echo.
echo ===========================================================================
echo  STEP 7/7  Launch the whole stack  (server + tunnel + Edge + bridge + UI)
echo ===========================================================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_all.ps1"

echo.
echo ===========================================================================
echo  Putting a one-click launcher on your Desktop...
echo ===========================================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_desktop_shortcut.ps1"

echo.
echo ===========================================================================
echo  Health check  (is every link green?)
echo ===========================================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0doctor.ps1"

echo.
echo ===========================================================================
echo  SETUP COMPLETE
echo ===========================================================================
echo   Daily startup : double-click  "M365 Companion"  on your Desktop
echo   Check anytime : double-click  doctor.bat        (all green = fully wired)
echo   Two windows opened: CopilotChat (talk to it) and FleetCockpit (watch runs)
echo   Any RED above?  doctor printed the exact fix for each line.
echo.
pause
endlocal
