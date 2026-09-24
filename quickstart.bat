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

REM THE INSTALL PATH, CHECKED BEFORE DELAYED EXPANSION IS ON (D23). With it on, a "!" in this
REM folder's path is silently dropped wherever %~dp0 is expanded, so every script below would be
REM looked for somewhere that does not exist. A UNC path cannot be made current by `cd /d`.
REM setup.bat carries the same check with the same wording.
REM LABELS, NOT BLOCKS, for the messages: the path is expanded with %...% and a folder such as
REM "companion (1)" -- what a second ZIP download is named -- would close a ( ) block early.
setlocal EnableExtensions DisableDelayedExpansion
set "QS_HERE=%~dp0"
if "%QS_HERE:~0,2%"=="\\" goto :qs_bad_unc
if not "%QS_HERE:!=%"=="%QS_HERE%" goto :qs_bad_bang
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

REM PSMODULEPATH, SANITIZED FOR EVERY "powershell" (5.1) CHILD BELOW (a3415bf). Same reason as
REM setup.bat's own copy of this comment: powershell.exe inherits THIS process's environment,
REM and a PowerShell 7 (pwsh) parent shell's PSModulePath makes Windows PowerShell 5.1 resolve
REM Get-AuthenticodeSignature to pwsh 7's own (CLR-incompatible) Microsoft.PowerShell.Security
REM and fail to load it -- silently turning STEP 4's devtunnel.exe Authenticode check into
REM "could not verify". `call "%~dp0setup.bat"` below runs in its own setlocal scope, so its
REM copy of this line does not survive back into this script's environment; this copy is what
REM protects the setup_devtunnel.ps1 invocation later in THIS file.
set "PSModulePath=%UserProfile%\Documents\WindowsPowerShell\Modules;%ProgramFiles%\WindowsPowerShell\Modules;%SystemRoot%\System32\WindowsPowerShell\v1.0\Modules"

REM OVERALL RESULT (SF-15, 2026-09-24). Several checks below (the health check especially) used
REM to route a failure to the same :after_banner tail as success, which then fell off the end of
REM the file with no `exit /b`, so cmd's own default (0) was reported regardless -- a caller
REM (a scheduled re-run, another script, an operator reading %ERRORLEVEL%) saw "success" over a
REM screen of FAIL lines. Every branch that finds a real problem sets this to non-zero before
REM jumping to :after_banner; the tail exits with it.
set "QS_EXIT=0"

REM ONE QUICKSTART AT A TIME (D21). Two at once ran pip into one .venv, could each write a .env
REM with DIFFERENT secrets (one window then shows values that are not the saved ones), and both
REM created the tunnel so the loser made a second one. The lock records this window's cmd.exe and
REM is released on every exit below; a window closed mid-run leaves a lock whose owner is gone,
REM which the next run takes over automatically (by checking whether the recorded PID is still
REM alive), so a stale lock never needs anyone to delete a file by hand.
REM FAILS CLOSED (INST-14, 2026-09-24): only errorlevel 0 -- POSITIVE, CONFIRMED ownership --
REM proceeds. Anything else stops here instead of installing unlocked, because "the lock helper
REM had a problem" and "another quickstart is running" look identical from this side, and
REM guessing wrong risks two installs writing different secrets into the same .env at once.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\quickstart_lock.ps1" acquire
set "LOCK_RC=%ERRORLEVEL%"
if "%LOCK_RC%"=="0" goto :lock_acquired
if "%LOCK_RC%"=="10" (
    REM Another quickstart is confirmed alive -- quickstart_lock.ps1 already printed its PID
    REM and what to do.
    pause
    exit /b 10
)
if "%LOCK_RC%"=="9009" (
    echo.
    echo ACTION NEEDED: PowerShell could not be started ^(not found on PATH^), so quickstart
    echo cannot confirm that no other quickstart is already installing into this folder.
    echo Refusing to continue rather than risk two installs writing different secrets into
    echo the same .env at once. Install/repair PowerShell on PATH, then run quickstart.bat again.
    pause
    exit /b 1
)
echo.
echo ACTION NEEDED: could not confirm that no other quickstart is already running for this
echo folder ^(the lock check exited with code %LOCK_RC% instead of confirming^). See the message
echo above, if any. Refusing to continue rather than risk two installs writing different
echo secrets into the same .env at once. Wait a few seconds and run quickstart.bat again -- a
echo lock left by a closed window clears itself automatically.
pause
exit /b 1
:lock_acquired

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
REM BY FULL PATH: with NoDefaultCurrentDirectoryInExePath set (a hardening some machines have),
REM cmd does not look in the current directory for a bare `setup.bat`, and STEP 1 died with
REM "'setup.bat' is not recognized". Found by the install-path tests, 2026-09-24.
call "%~dp0setup.bat"
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
    call :release_lock
    pause
    exit /b %BOOT_RC%
)
REM The interpreter STEP 1 just verified. The .env edits below go through scripts\env_file.py,
REM which replaces .env atomically (D28); cmd cannot, and a truncated .env costs the Bearer token.
set "QS_PY=.venv\Scripts\python.exe"

echo.
echo ===========================================================================
echo  STEP 2/7  Your secrets - copy these into your MCP client
echo ===========================================================================
REM D1: the protected-password line used to say "shown by setup when generated" -- i.e. gone,
REM once that window had scrolled or closed. It now names the command that shows it again.
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if /i "%%A"=="MCP_API_KEY" echo   Bearer token  ^(MCP_API_KEY^)        : %%B
        if /i "%%A"=="MCP_UNLOCK_PASSWORD" echo   Unlock password ^(MCP_UNLOCK_PASSWORD^): %%B
        if /i "%%A"=="MCP_UNLOCK_PASSWORD_PROTECTED" echo   Unlock password                  : ^<stored protected; double-click copilot_studio_values.bat to show it^>
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
    set "FETCH_FAILED="
    set "NO_UPSTREAM="
    REM THE PROXY, FOR GIT ONLY (D10). git does not read the proxy Windows is configured with;
    REM passed with -c so it reaches this fetch and pull and nothing else this window starts.
    set "QS_GIT_PROXY="
    if not defined HTTPS_PROXY for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\detect_proxy.ps1" 2^>nul`) do set "QS_GIT_PROXY=-c http.proxy=%%P"
    git !QS_GIT_PROXY! fetch --quiet
    REM READ IT (D25). A failed fetch -- no network, proxy, expired credentials -- was ignored,
    REM and the count below then read the STALE remote-tracking ref and printed "Up to date."
    if errorlevel 1 set "FETCH_FAILED=1"
    git rev-parse --verify --quiet "@{u}" >nul 2>nul
    if errorlevel 1 set "NO_UPSTREAM=1"
    set "BEHIND=0"
    for /f %%C in ('git rev-list --count HEAD..@{u} 2^>nul') do set "BEHIND=%%C"
    if defined FETCH_FAILED (
        echo   COULD NOT CHECK FOR UPDATES: 'git fetch' failed -- its error is just above.
        echo   This is NOT "up to date"; the check did not happen. Everything below runs the
        echo   code you have now. Check the network or proxy, then re-run to check again.
    ) else if defined NO_UPSTREAM (
        echo   This branch does not track a remote branch, so there is nothing to compare with.
        echo   Skipping the update check.
    ) else if "!BEHIND!"=="0" (
        echo   Up to date.
    ) else (
        echo   !BEHIND! update^(s^) available on the remote branch.
        set /p ANS="   Pull them now with a fast-forward? [y/N] "
        REM DECIDE HERE, ACT OUTSIDE. Labels are not valid inside a parenthesised block --
        REM cmd rejects the whole file with ") was unexpected at this time" -- so the answer
        REM becomes a flag and everything that acts on it happens after the blocks close.
        if /i "!ANS!"=="y" set "DO_PULL=1"
        if not "!ANS!"=="y" echo   Skipped. You can pull later with: git pull --ff-only
    )
)

if defined DO_PULL (
    git !QS_GIT_PROXY! pull --ff-only
    REM READ THE RESULT. A pull that fails -- dirty tree, diverged branch, no network -- printed
    REM its error among everything else and the run carried on, so the operator believed they
    REM were current while running the old code.
    if errorlevel 1 (
        echo.
        echo   UPDATE FAILED -- this checkout is still on the older version. The error is just
        echo   above. Everything below continues to run from the code you have now.
    ) else (
        REM AND A SUCCESSFUL PULL REWRITES THIS FILE WHILE CMD IS EXECUTING IT. cmd resumes a
        REM batch file from a byte offset after every line, so replacing it underneath a running
        REM instance continues at whatever now occupies that offset -- a partial line, the middle
        REM of another block, or nothing. Undefined, and silent. Stopping is the only safe move
        REM once the script has changed itself.
        echo.
        echo ===========================================================================
        echo  Updated. Please run quickstart.bat again.
        echo ===========================================================================
        echo   The update replaced this script while it was running, so it cannot safely
        echo   continue in this window. Nothing is lost -- it resumes where it left off.
        echo.
        REM INLINE, NOT `call :release_lock`: a call looks its label up in the file on disk,
        REM which is now the NEW file. This whole block was parsed before the pull, so a command
        REM written here is safe; a jump is not.
        powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\quickstart_lock.ps1" release >nul 2>nul
        pause
        exit /b 0
    )
)

echo.
echo ===========================================================================
echo  How should Copilot Studio be allowed to reach this machine?
echo ===========================================================================
echo   The tunnel needs an access grant or NOTHING can connect to it -- the
echo   connection test in STEP 5 will fail. Asked before the tunnel is created,
echo   because the grant is part of creating it.
echo.
echo   [A] Anonymous  - anyone who knows the URL can reach the server. The file
echo                    and shell tools are then on the public internet, gated
echo                    ONLY by your Bearer token. Simplest, and least private.
echo   [T] Tenant     - only accounts in the Entra tenant of the Microsoft account
echo                    you sign devtunnel in with in STEP 4. More restrictive.
echo   [N] Neither    - decide later. Copilot Studio will NOT connect until you do.
echo.
set "TUNNEL_ACCESS="
set "TENANT_ID="
choice /C ATN /N /M "   Press A, T or N: "
REM Read errorlevel IMMEDIATELY -- choice sets it (A=1, T=2, N=3) and any command
REM in between resets it. Delayed expansion is on, so !ERRORLEVEL! is the runtime value.
if "!ERRORLEVEL!"=="1" set "TUNNEL_ACCESS=anonymous"
if "!ERRORLEVEL!"=="2" set "TUNNEL_ACCESS=tenant"
if "!ERRORLEVEL!"=="3" set "TUNNEL_ACCESS=none"
REM MEASURED: with stdin closed, `choice` prints "ERROR: The file is either empty or does not
REM contain the valid choices" and sets none of the above, leaving this empty. The effect was
REM already safe -- nothing is granted -- but nothing SAID so either, and the recorded decision
REM was a blank. An absent answer is the same answer as N, and is now written down as one.
if "!TUNNEL_ACCESS!"=="" set "TUNNEL_ACCESS=none"
REM NO TENANT ID IS ASKED ANY MORE (D16). `devtunnel access create --help` (CLI 1.0.1516) shows
REM --tenant is a FLAG meaning "the signed-in account's tenant"; it takes no id, so the GUID this
REM prompted for was never usable and setup_devtunnel.ps1 no longer passes one. A non-empty
REM -TenantId now only SELECTS tenant mode there, so a fixed word is passed and the screen says
REM which tenant that is. Flattened, one statement per line, as before.
if not "!TUNNEL_ACCESS!"=="tenant" goto :after_tenant_id
set "TENANT_ID=signed-in-account"
echo   Tenant access = the Entra tenant of the account you sign devtunnel in with in STEP 4.
echo   No id is needed; devtunnel grants that account's own tenant.
:after_tenant_id
REM RECORD THE DECISION, NOT THE ACT -- the same pattern as the convenience block below, and for
REM the same reason: the absence of a decision must never be read as consent to expose anything.
if not exist ".setup" mkdir ".setup"
> ".setup\tunnel_access_choice" echo access=!TUNNEL_ACCESS!
REM REPLACED, NOT APPENDED (A). Get-AllowAnonymous takes the FIRST matching line and breaks, so
REM an older MCP_TUNNEL_ALLOW_ANONYMOUS=0 further up the file would keep winning and the operator
REM would be told the choice was recorded while nothing had changed. env_file.py replaces the
REM first assignment, drops duplicates, reads and writes UTF-8 without a BOM (a Japanese comment
REM line round-trips unchanged) and swaps the file in atomically (D28).
REM REMOVED (N, T) -- D4. Only A ever wrote the key and nothing ever took it out, so choosing N or
REM T after an earlier A left MCP_TUNNEL_ALLOW_ANONYMOUS=1 in .env, setup_devtunnel.ps1 read it,
REM and the tunnel STAYED open to the anonymous internet while this screen said "no grant yet".
REM The key is removed, this window's copy of the variable is cleared so STEP 4 cannot inherit
REM it, and the message says what STEP 4 does about a grant already on the tunnel.
set "ANON_REMOVED="
if "!TUNNEL_ACCESS!"=="anonymous" goto :access_anonymous
if defined MCP_TUNNEL_ALLOW_ANONYMOUS (
    echo   NOTE: your Windows environment sets MCP_TUNNEL_ALLOW_ANONYMOUS. This run ignores it,
    echo   but other launches would not. Remove it with Windows Settings, Edit environment
    echo   variables for your account, so nothing grants anonymous access behind your back.
)
set "MCP_TUNNEL_ALLOW_ANONYMOUS="
for /f "usebackq delims=" %%R in (`"!QS_PY!" scripts\env_file.py unset MCP_TUNNEL_ALLOW_ANONYMOUS 2^>nul`) do set "ANON_REMOVED=%%R"
if "!ANON_REMOVED!"=="removed" echo   Removed MCP_TUNNEL_ALLOW_ANONYMOUS from .env ^(an earlier run had chosen anonymous^).
if not "!ANON_REMOVED!"=="removed" if not "!ANON_REMOVED!"=="absent" (
    echo   WARNING: could not update .env to remove MCP_TUNNEL_ALLOW_ANONYMOUS. Open .env and
    echo   delete that line by hand, then re-run quickstart.bat -- until then STEP 4 may keep
    echo   the tunnel open to anyone with its URL.
)
echo   Any anonymous access already granted on the tunnel by an earlier run is REVOKED by
echo   STEP 4 ^(setup_devtunnel^) now, so the tunnel is not left open to the internet.
if "!TUNNEL_ACCESS!"=="none" (
    echo   Recorded: no access grant. STEP 5's connection test will fail until you re-run
    echo   quickstart.bat and choose A or T.
)
goto :after_access_write
:access_anonymous
"!QS_PY!" scripts\env_file.py set MCP_TUNNEL_ALLOW_ANONYMOUS 1
if errorlevel 1 (
    echo   WARNING: could not write MCP_TUNNEL_ALLOW_ANONYMOUS=1 to .env ^(error above^). This run
    echo   still grants anonymous access; later runs will not remember the choice.
) else (
    echo   Recorded: anonymous access. ^(MCP_TUNNEL_ALLOW_ANONYMOUS=1 in .env^)
)
:after_access_write

echo ===========================================================================
echo  STEP 4/7  Dev Tunnel  (install + sign-in + tunnel + public URL)
echo ===========================================================================
echo   Installs the devtunnel CLI (winget or direct download), signs you in
echo   (browser or device code), creates the tunnel, and prints the PUBLIC URL.
echo.
set "ANON_FLAG="
if "!TUNNEL_ACCESS!"=="anonymous" set "ANON_FLAG=-ForceAnonymous"
REM PASSED, not left to be re-derived. The file and the environment can both disagree with the
REM answer just given; the answer wins for this run.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_devtunnel.ps1" -TenantId "!TENANT_ID!" !ANON_FLAG!
REM Capture the Dev Tunnel setup exit code BEFORE any other command: a plain
REM `set` succeeds and would RESET errorlevel to 0, so we must grab it first.
set "DT_RC=%ERRORLEVEL%"
if not "%DT_RC%"=="0" (
    echo.
    echo Dev Tunnel setup did not finish. Read the message above, fix it, then
    echo run quickstart.bat again.
    call :release_lock
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
    call :release_lock
    pause
    exit /b 1
)

echo.
echo ===========================================================================
echo  Convenience setup  (asked BEFORE anything is created)
echo ===========================================================================
echo   These two change your machine outside this folder, so they are asked
echo   first and your answer is recorded. Say no and nothing is created.
echo.
set "PROV_SHORTCUT=no"
set "PROV_AUTOSTART=no"
set /p MKLNK="   Create a one-click 'M365 Companion' launcher on your Desktop? [Y/n] "
if /i not "!MKLNK!"=="n" set "PROV_SHORTCUT=yes"
set /p MKAUTO="   Start the background supervisor automatically when you log on? [y/N] "
if /i "!MKAUTO!"=="y" set "PROV_AUTOSTART=yes"

REM RECORD THE DECISION, NOT THE ACT. start_all.ps1 provisions from this marker, so it must be
REM written BEFORE the first start_all call below (the -CoreOnly launch), not after -- otherwise
REM the question is put to someone whose answer can no longer matter. start_all.ps1 provisioned
REM both whenever this marker was ABSENT, so the absence of a decision was read as consent.
REM Logon autostart was never asked about at all.
if not exist ".setup" mkdir ".setup"
> ".setup\convenience_provisioned" echo shortcut=!PROV_SHORTCUT!
>> ".setup\convenience_provisioned" echo autostart=!PROV_AUTOSTART!
if /i "!PROV_SHORTCUT!"=="yes" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\make_desktop_shortcut.ps1"
) else (
    echo   Desktop launcher skipped. Create it later with scripts\make_desktop_shortcut.ps1
)
if /i "!PROV_AUTOSTART!"=="yes" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\register-supervisor.ps1"
    REM You asked for logon autostart and it may not have been created. Without this the
    REM failure surfaces at the NEXT logon, as the stack simply not being there.
    if errorlevel 1 (
        echo   Logon autostart could NOT be registered -- see the ERROR line above. Everything
        echo   else continues; start the stack from the Desktop launcher until this is fixed.
    )
) else (
    echo   Logon autostart skipped. Register it later with scripts\register-supervisor.ps1
)

echo.
echo ===========================================================================
echo  Starting the MCP server  ^(STEP 5 asks you to test a connection to it^)
echo ===========================================================================
REM THE CONNECTION TEST IN STEP 5 NEEDS A SERVER. copilot_studio_values.ps1 tells the reader to
REM press "Add connection / Test" and watch the tool list load, and until now the server was not
REM launched until STEP 7 -- so the first thing that happens on the only manual step of the whole
REM install was a connection error. -CoreOnly starts the supervisor (server + tunnel host) and
REM nothing else; STEP 7's full run is idempotent and leaves it alone.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_all.ps1" -CoreOnly
REM READ IT. start_all reports its problems in prose and used to exit 0 regardless, so a launch
REM that failed and one that worked were indistinguishable from here -- and this is the launch
REM STEP 5's connection test depends on.
if errorlevel 1 (
    echo.
    echo   The core startup reported problem^(s^) -- see the lines above. The wait below will
    echo   say whether the server came up anyway.
)
echo.
echo   Waiting for the MCP server to answer (up to 90s)...
powershell -NoProfile -Command "$sw = [Diagnostics.Stopwatch]::StartNew(); $ok = $false; while ($sw.Elapsed.TotalSeconds -lt 90) { try { if ((Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 3 -UseBasicParsing).StatusCode -eq 200) { $ok = $true; break } } catch { }; Start-Sleep -Seconds 2 }; if ($ok) { Write-Host ('   Server answered after {0:N0}s -- the connection test in the next step will work.' -f $sw.Elapsed.TotalSeconds) } else { Write-Host '   Server did not answer within 90s. Continue with STEP 5 anyway; the health check at the end says why.' }"

REM STEP 5 (Copilot Studio) and STEP 6 (paste agent URLs) are the manual leg. If
REM .env already carries a non-empty agent URL, that leg was done on a previous
REM run -- offer to skip straight to STEP 7 (launch). `..*` requires at least one
REM character after `=`, so a blank `MCP_IMPL_AGENT_URL=` line does NOT count as
REM configured (same pattern style as the MCP_TUNNEL_URL gate in STEP 4).
set "SKIP56="
REM IMPL ALONE. `findstr /r` treats the space as OR -- measured: FLEET only also exits 0 --
REM so a machine with just the fleet URL skipped the step that sets the REQUIRED one and
REM called itself configured. The fleet URL is optional and falls back to this very key.
findstr /b /r "MCP_IMPL_AGENT_URL=..*" ".env" >nul 2>nul
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
    REM READ WHAT IT REPORTED. configure_env.ps1 uses distinct codes on purpose -- its own
    REM comment says so -- and they were being discarded, so a cancelled dialog and a saved one
    REM were indistinguishable from here. Checked highest-first because `if errorlevel N` is
    REM "N or greater", and read immediately: any command in between resets it.
    if errorlevel 4 set "CFG_DIALOG_BLOCKED=1"
    if errorlevel 4 (
        echo   The dialog could not run on this machine -- that is usually AppLocker blocking
        echo   the WinForms assembly. Asking here instead.
    ) else if errorlevel 3 (
        echo   Saved, but the main agent URL was left BLANK -- chat and fleet will not work
        echo   until it is set. Re-run configure_env.bat when you have it.
    ) else if errorlevel 2 (
        echo   The dialog was CANCELLED -- .env was not changed and the agent URL is still
        echo   unset. Re-run configure_env.bat when you have it.
    )
) else (
    echo.
    echo   Skipping STEP 5/6 ^(agent already configured^). Jumping to STEP 7 launch.
)

REM CONSOLE FALLBACK FOR A BLOCKED DIALOG. Exit 4 means Add-Type/WinForms was refused -- the
REM script's own comment names AppLocker on managed machines -- and the only way out was "edit
REM .env by hand", which an installer cannot rely on. cmd can ask for a line of text anywhere.
REM Only the required key; the fleet URL is optional and falls back to the main one.
REM Flattened deliberately: `set /p` inside a parenthesised block did not settle before the `if`
REM that read it, and a label inside one makes cmd reject the entire file.
if not defined CFG_DIALOG_BLOCKED goto :after_cfg_fallback
findstr /b /r "MCP_IMPL_AGENT_URL=..*" ".env" >nul 2>nul
if not errorlevel 1 goto :after_cfg_fallback
echo.
echo   Paste the main agent URL (the address bar URL when the agent is open in
echo   M365 Copilot). Leave it blank to set it later with configure_env.bat.
set "IMPL_URL="
set /p IMPL_URL="   MCP_IMPL_AGENT_URL: "
if "!IMPL_URL!"=="" echo   Left unset. Chat and fleet will not work until it is set.
REM SET, NOT APPENDED, AND ATOMIC (D28). The old append added a second MCP_IMPL_AGENT_URL line when
REM a blank one was already there (readers disagree on first- vs last-wins), and a pasted URL
REM containing an apostrophe broke the PowerShell string it was spliced into. env_file.py takes
REM the value as an argument, never as code, and replaces .env in one rename.
if "!IMPL_URL!"=="" goto :after_cfg_fallback
"!QS_PY!" scripts\env_file.py set MCP_IMPL_AGENT_URL "!IMPL_URL!"
if errorlevel 1 (
    echo   Could NOT save it to .env ^(error above^). Run configure_env.bat to set it.
) else (
    echo   Saved to .env.
)
:after_cfg_fallback

echo.
echo ===========================================================================
echo  STEP 7/7  Launch the whole stack  (server + tunnel + Edge + bridge + UI)
echo ===========================================================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_all.ps1"
set "START_ALL_BAD=%ERRORLEVEL%"
REM Captured immediately: any command in between resets it -- the trap this file documents in
REM the two places it was already hit.
if not "%START_ALL_BAD%"=="0" (
    echo.
    echo   Startup reported %START_ALL_BAD% problem^(s^). They are listed just above; the health
    echo   check below says what each one means for the finished setup.
)

REM WAIT FOR THE SERVER BEFORE MEASURING IT. start_all returns once the supervisor PROCESS
REM exists; the server it launches has to import and bind after that. Checking health at the
REM moment start_all returns produced the three red lines -- server down, tunnel not serving,
REM Bearer rejected -- on machines where nothing was wrong except the question being asked too
REM early. Bounded, and it says what it is waiting for: a silent pause reads as a hang.
echo.
echo   Waiting for the MCP server to answer (up to 90s)...
powershell -NoProfile -Command "$ok = $false; for ($i = 0; $i -lt 45; $i++) { try { if ((Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 2 -UseBasicParsing).StatusCode -eq 200) { $ok = $true; break } } catch { }; Start-Sleep -Seconds 2 }; if ($ok) { Write-Host ('   Server answered after about ' + ($i * 2) + 's.') } else { Write-Host '   Server did not answer within 90s -- the health check below will say why.' }"

echo.
echo ===========================================================================
echo  Sign in to M365 on the companion browser
echo ===========================================================================
echo   Checking in the background. The browser window is only brought forward if
echo   you actually need to sign in - it will not interrupt you otherwise.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\ensure_m365_signin.ps1"
REM EXIT CODE INTENTIONALLY NOT CHECKED. Unlike devtunnel/configure_env/start_all/doctor,
REM whose distinct codes a person has to act on, ensure_m365_signin.ps1 always exits 0 BY
REM DESIGN (see its "NEVER FAIL THE WHOLE SETUP OVER THIS" block): a not-yet-completed
REM sign-in is resumable, the wrapper prints its own "(sign-in not completed yet...)" line,
REM and the health check that runs immediately below reports the sign-in state either way.
REM A checked errorlevel here would only ever read 0, so there is nothing to branch on.

echo.
echo ===========================================================================
echo  Health check  (is every link green?)
echo ===========================================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\doctor.ps1"
REM READ THE RESULT. doctor exits with the number of FAILED checks and this was thrown away,
REM so "SETUP COMPLETE" printed over a screen of red. Two other steps in this file already
REM check exit codes, so ignoring it here was an inconsistency, not a convention. Optional
REM checks no longer count toward that number, so a non-zero value now means something a
REM person has to act on.
set "DOCTOR_BAD=%ERRORLEVEL%"

echo.
if not "!DOCTOR_BAD!"=="0" (
    echo ===========================================================================
    echo  SETUP INCOMPLETE - !DOCTOR_BAD! check^(s^) failed
    echo ===========================================================================
    echo   The lines marked FAIL above each printed their own fix.
    echo   Anything marked WARN is optional and does not block you.
    echo.
    REM SHOW THE SERVER'S OWN ERROR HERE. Naming a log file is the same delegation one layer
    REM down, and this is the only install method there is - if quickstart cannot say why the
    REM server died, nobody can. The supervisor relaunches it on a loop, so re-running
    REM start_all.bat would change nothing either.
    REM ONLY WHEN THE SERVER IS THE THING THAT IS DOWN. This block used to fire on ANY red
    REM line, so an unrelated failure -- a tunnel name, a missing agent URL -- printed "the
    REM MCP server tried to start and stopped" over a server that was running fine, quoting
    REM uvicorn's own startup lines as evidence. It now probes /health first, and says
    REM plainly when the only output on record is from a launch that succeeded.
    powershell -NoProfile -Command "$up = $false; try { $up = (Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 4 -UseBasicParsing).StatusCode -eq 200 } catch { }; if (-not $up) { $d = Join-Path (Get-Location) '.setup\logs'; foreach ($p in @((Join-Path $d 'server.err.log'), (Join-Path $d 'server.err.history.log'))) { if ((Test-Path $p) -and (Get-Item $p).Length -gt 0) { $t = Get-Content -Tail 25 $p; $ok = ($t -match 'Application startup complete') -or ($t -match 'Uvicorn running on'); Write-Host '   ---------------------------------------------------------------'; if ($ok) { Write-Host '   The MCP server is not answering, and the only output on record is from'; Write-Host '   a launch that STARTED SUCCESSFULLY -- it does not explain this failure.' } else { Write-Host '   The MCP server tried to start and stopped. Its last output was:' }; Write-Host '   ---------------------------------------------------------------'; $t; Write-Host '   ---------------------------------------------------------------'; break } } }"
    echo.
    echo   Fix what is shown above, then run quickstart.bat again.
    echo   It resumes from where it stopped - nothing is repeated unnecessarily.
    set "QS_EXIT=!DOCTOR_BAD!"
    goto :after_banner
)
REM "COULD NOT DETERMINE" IS NOT "COMPLETE". doctor's exit code is the number of FAILURES, and
REM a required check that could not be answered is neither a failure nor a completion -- it used
REM to land in the same counter as an absent optional component and print COMPLETE over it.
set "DOCTOR_UNKNOWN=0"
for /f "tokens=2 delims==" %%U in ('findstr /b "unknown=" ".setup\logs\doctor_summary.txt" 2^>nul') do set "DOCTOR_UNKNOWN=%%U"
if not "!DOCTOR_UNKNOWN!"=="0" (
    echo ===========================================================================
    echo  SETUP NOT CONFIRMED - !DOCTOR_UNKNOWN! required check^(s^) could not be answered
    echo ===========================================================================
    echo   Nothing failed, but something required could not be determined -- look for
    echo   the [WARN] lines above; each printed what to retry. Re-run quickstart.bat
    echo   once that is resolved. It resumes; nothing is repeated unnecessarily.
    set "QS_EXIT=1"
    goto :after_banner
)
echo ===========================================================================
echo  SETUP COMPLETE
echo ===========================================================================
echo   Daily startup : double-click  "M365 Companion"  on your Desktop
echo   Check anytime : double-click  doctor.bat        (all green = fully wired)
echo   Two windows opened: CopilotChat (talk to it) and FleetCockpit (watch runs)
echo   Optional Deep Review: MCP_REVIEW_P2C=1 ^(deep^) or 2 ^(full validation^), plus MCP_EXECUTION_PROFILES=1
echo   in .env, then re-run start_all.bat. It uses headless LOCAL_LOOP by default.
echo   Any RED above?  doctor printed the exact fix for each line.
:after_banner
REM Released BEFORE the final pause: the work is finished, and a window left open at this prompt
REM must not make the next quickstart refuse to start.
call :release_lock
echo.
pause
REM `exit /b` unwinds the setlocal scopes on its own (every other early exit in this file already
REM relies on that, never calling `endlocal` first) -- an explicit `endlocal` here would instead
REM discard QS_EXIT before it could be read, which is how this used to fall through to `goto
REM :eof` and report 0 (success) after printing a screen of FAIL lines (SF-15, 2026-09-24).
exit /b %QS_EXIT%

REM ---- subroutines and early exits (never reached by falling through) -----------------------
:release_lock
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\quickstart_lock.ps1" release >nul 2>nul
exit /b 0

:qs_bad_unc
echo.
echo ACTION NEEDED: this folder is on a network path: "%QS_HERE%"
echo   Windows' command prompt cannot run setup from a \\server\share location.
echo   Copy the whole folder to a local drive, for example
echo       %USERPROFILE%\m365-copilot-companion
echo   and run quickstart.bat from there.
pause
exit /b 1

:qs_bad_bang
echo.
echo ACTION NEEDED: the folder path contains a "!" character:
echo       "%QS_HERE%"
echo   The command prompt drops "!" from paths inside these setup scripts, so every file
echo   would be looked for in the wrong place. Rename the folder ^(or move it^) to a path
echo   without "!", for example %USERPROFILE%\m365-copilot-companion, and run it from there.
pause
exit /b 1
