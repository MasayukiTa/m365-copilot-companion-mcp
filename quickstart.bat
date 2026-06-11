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
echo  STEP 1/4  Install Python, venv and requirements (resumable bootstrap)
echo ===========================================================================
call setup.bat
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
echo  STEP 2/4  Your secrets - copy these into your MCP client
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
echo  STEP 3/4  Check git for updates (fetch only - never pushes)
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
echo  STEP 4/4  Starting the MCP server  (Ctrl+C to stop)
echo ===========================================================================
echo.
%PYEXE% main.py
endlocal
