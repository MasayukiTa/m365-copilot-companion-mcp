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
echo   SAFE TO RE-RUN: this script resumes where it left off.
echo   Completed steps are skipped or fast -- so re-running after an interruption
echo   (a failed sign-in, a closed window, a reboot) just picks up from there.

echo.
echo ===========================================================================
echo  STEP 1/7  Install Python, venv and requirements (resumable bootstrap)
echo ===========================================================================
REM Tell setup.bat it is being CALLED (not double-clicked), so it does not add
REM its own pause -- quickstart has its own pauses and a final one.
set "FROM_QUICKSTART=1"
call setup.bat
REM Capture the bootstrap exit code BEFORE any other command: the following
REM `set` succeeds and would otherwise RESET errorlevel to 0, so `if errorlevel 1`
REM never fired and a failed/paused bootstrap (e.g. devtunnel sign-in needed,
REM rc=2) was silently ignored and the run continued on a half-set-up env.
set "BOOT_RC=%ERRORLEVEL%"
set "FROM_QUICKSTART="
if not "%BOOT_RC%"=="0" (
    echo.
    echo Bootstrap stopped with exit code %BOOT_RC%. Read the message above for
    echo the exact action needed, then run quickstart.bat again to resume.
    pause
    exit /b %BOOT_RC%
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
        if /i "%%A"=="MCP_API_KEY" echo   Bearer token  ^(MCP_API_KEY^)        : %%B
        if /i "%%A"=="MCP_UNLOCK_PASSWORD" echo   Unlock password ^(MCP_UNLOCK_PASSWORD^): %%B
        if /i "%%A"=="MCP_UNLOCK_PASSWORD_PROTECTED" echo   Unlock password                  : ^<protected in .env; shown by setup when generated^>
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
    echo   Not a git checkout - skipping git update check.
    if exist "update.bat" (
        echo   ZIP install detected. To update without git, run: update.bat
    )
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
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_devtunnel.ps1"
REM Capture the Dev Tunnel setup exit code BEFORE any other command: a plain
REM `set` succeeds and would RESET errorlevel to 0, so we must grab it first.
set "DT_RC=%ERRORLEVEL%"
if not "%DT_RC%"=="0" (
    echo.
    echo Dev Tunnel setup did not finish. Read the message above, fix it, then
    echo run quickstart.bat again.
    pause
    exit /b %DT_RC%
)
REM STEP 5 (Copilot Studio) needs the PUBLIC tunnel URL, so confirm STEP 4
REM actually recorded a non-empty MCP_TUNNEL_URL in .env before continuing.
REM `..*` requires at least one character after the `=`, so a blank
REM `MCP_TUNNEL_URL=` line does NOT count as ready.
findstr /b /r "MCP_TUNNEL_URL=..*" ".env" >nul 2>nul
if errorlevel 1 (
    echo.
    echo Dev Tunnel URL is not ready -- STEP 5 needs it. Re-run quickstart.bat
    echo after fixing STEP 4.
    pause
    exit /b 1
)

REM STEP 5 (Copilot Studio) and STEP 6 (paste agent URLs) are the manual leg. If
REM .env already carries a non-empty agent URL, that leg was done on a previous
REM run -- offer to skip straight to STEP 7 (launch). `..*` requires at least one
REM character after `=`, so a blank `MCP_IMPL_AGENT_URL=` line does NOT count as
REM configured (same pattern style as the MCP_TUNNEL_URL gate in STEP 4).
set "SKIP56="
findstr /b /r "MCP_IMPL_AGENT_URL=..* MCP_FLEET_AGENT_URL=..*" ".env" >nul 2>nul
if not errorlevel 1 (
    echo.
    echo   An agent URL is already configured -- Copilot Studio step appears DONE.
    choice /C SR /N /M "   Press S to skip to STEP 7 (launch), or R to redo STEP 5/6: "
    REM Capture errorlevel IMMEDIATELY: choice sets it (S=1, R=2) and any command
    REM in between would reset it. We are inside a parenthesized block with delayed
    REM expansion ON, so read !ERRORLEVEL! (runtime value) -- %ERRORLEVEL% would be
    REM the parse-time value captured before choice ran (the classic block pitfall).
    if "!ERRORLEVEL!"=="1" set "SKIP56=1"
)

if not defined SKIP56 (
    echo.
    echo ===========================================================================
    echo  STEP 5/7  Copilot Studio  ^(the ONLY manual, by-hand step^)
    echo ===========================================================================
    echo   Add an MCP connector in Copilot Studio, then create your companion agent.
    echo   The 3 EXACT values to paste are printed below ^(full guide: README STEP 4^):
    echo.
    echo   Opening Copilot Studio ^(https://copilotstudio.microsoft.com/^) in your browser...
    start "" "https://copilotstudio.microsoft.com/"
    echo.
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\copilot_studio_values.ps1"
    echo   After creating the agent, open it in M365 Copilot and copy its address-bar
    echo   URL -- you will paste it into a dialog in the next step.
    echo.
    echo   Press any key AFTER you have created the agent in Copilot Studio...
    pause >nul

    echo.
    echo ===========================================================================
    echo  STEP 6/7  Paste your agent URLs  ^(a dialog window opens^)
    echo ===========================================================================
    echo   Paste the agent URL^(s^) into the dialog and click Save. Leave blank any
    echo   you do not have yet -- you can re-run configure_env.bat later to add them.
    echo.
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\configure_env.ps1"
) else (
    echo.
    echo   Skipping STEP 5/6 ^(agent already configured^). Jumping to STEP 7 launch.
)

echo.
echo ===========================================================================
echo  STEP 7/7  Launch the whole stack  (server + tunnel + Edge + bridge + UI)
echo ===========================================================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_all.ps1"

echo.
echo ===========================================================================
echo  Desktop launcher  (optional, recommended)
echo ===========================================================================
set /p MKLNK="   Create a one-click 'M365 Companion' launcher on your Desktop? [Y/n] "
if /i "!MKLNK!"=="n" (
    echo   Skipped. You can create it later by running scripts\make_desktop_shortcut.ps1
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\make_desktop_shortcut.ps1"
)

echo.
echo ===========================================================================
echo  Health check  (is every link green?)
echo ===========================================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\doctor.ps1"

echo.
echo ===========================================================================
echo  SETUP COMPLETE
echo ===========================================================================
echo   Daily startup : double-click  "M365 Companion"  on your Desktop
echo   Check anytime : double-click  doctor.bat        (all green = fully wired)
echo   Two windows opened: CopilotChat (talk to it) and FleetCockpit (watch runs)
echo   Optional Deep Review: MCP_REVIEW_P2C=1 ^(deep^) or 2 ^(full validation^), plus MCP_EXECUTION_PROFILES=1
echo   in .env, then re-run start_all.bat. It uses headless LOCAL_LOOP by default.
echo   Any RED above?  doctor printed the exact fix for each line.
echo.
pause
endlocal
