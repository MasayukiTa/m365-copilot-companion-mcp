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
        REM DECIDE HERE, ACT OUTSIDE. Labels are not valid inside a parenthesised block --
        REM cmd rejects the whole file with ") was unexpected at this time" -- so the answer
        REM becomes a flag and everything that acts on it happens after the blocks close.
        if /i "!ANS!"=="y" set "DO_PULL=1"
        if not "!ANS!"=="y" echo   Skipped. You can pull later with: git pull --ff-only
    )
)

if defined DO_PULL (
    git pull --ff-only
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
echo   [T] Tenant     - only accounts in your Entra tenant. You will be asked for
echo                    the tenant id. More restrictive; needs that id to hand.
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
REM FLATTENED ON PURPOSE. `set /p` inside a parenthesized block did not settle before the `if`
REM that reads it, so an empty answer was not detected. One statement per line, no block.
if not "!TUNNEL_ACCESS!"=="tenant" goto :after_tenant_id
set /p TENANT_ID="   Entra tenant id (GUID): "
if "!TENANT_ID!"=="" echo   No tenant id given -- treating this as 'decide later'.
if "!TENANT_ID!"=="" set "TUNNEL_ACCESS=none"
:after_tenant_id
REM RECORD THE DECISION, NOT THE ACT -- the same pattern as the convenience block below, and for
REM the same reason: the absence of a decision must never be read as consent to expose anything.
if not exist ".setup" mkdir ".setup"
> ".setup\tunnel_access_choice" echo access=!TUNNEL_ACCESS!
if "!TUNNEL_ACCESS!"=="anonymous" (
    REM REPLACED, NOT APPENDED. Get-AllowAnonymous takes the FIRST matching line and breaks, so
    REM an older MCP_TUNNEL_ALLOW_ANONYMOUS=0 further up the file would keep winning and the
    REM operator would be told the choice was recorded while nothing had changed.
    powershell -NoProfile -Command "$p = Join-Path (Get-Location) '.env'; $keep = @(); if (Test-Path $p) { $keep = @(Get-Content $p | Where-Object { $_ -notmatch '^\s*MCP_TUNNEL_ALLOW_ANONYMOUS\s*=' }) }; $keep += 'MCP_TUNNEL_ALLOW_ANONYMOUS=1'; Set-Content -Path $p -Value $keep -Encoding ASCII"
    echo   Recorded: anonymous access. ^(MCP_TUNNEL_ALLOW_ANONYMOUS=1 in .env^)
)
if "!TUNNEL_ACCESS!"=="none" (
    echo   Recorded: no grant yet. STEP 5's connection test will fail until you
    echo   re-run quickstart.bat and choose A or T.
)

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
REM Same reason: a joined line would take MCP_API_KEY with it.
if not "!IMPL_URL!"=="" powershell -NoProfile -Command "$p = Join-Path (Get-Location) '.env'; $b = [IO.File]::ReadAllBytes($p); if ($b.Length -gt 0 -and $b[$b.Length-1] -ne 10) { [IO.File]::AppendAllText($p, [Environment]::NewLine) }; [IO.File]::AppendAllText($p, 'MCP_IMPL_AGENT_URL=!IMPL_URL!' + [Environment]::NewLine)"
if not "!IMPL_URL!"=="" echo   Saved to .env.
:after_cfg_fallback

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

REM RECORD THE DECISION, NOT THE ACT. start_all.ps1 provisioned both whenever this marker was
REM ABSENT, so the absence of a decision was read as consent -- and the question below used to
REM be asked AFTER the shortcut had already been created, which made answering "n" do nothing.
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
    powershell -NoProfile -Command "$up = $false; try { $up = (Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 4 -UseBasicParsing).StatusCode -eq 200 } catch { }; if (-not $up) { $d = '%~dp0.setup\logs'; foreach ($p in @((Join-Path $d 'server.err.log'), (Join-Path $d 'server.err.history.log'))) { if ((Test-Path $p) -and (Get-Item $p).Length -gt 0) { $t = Get-Content -Tail 25 $p; $ok = ($t -match 'Application startup complete') -or ($t -match 'Uvicorn running on'); Write-Host '   ---------------------------------------------------------------'; if ($ok) { Write-Host '   The MCP server is not answering, and the only output on record is from'; Write-Host '   a launch that STARTED SUCCESSFULLY -- it does not explain this failure.' } else { Write-Host '   The MCP server tried to start and stopped. Its last output was:' }; Write-Host '   ---------------------------------------------------------------'; $t; Write-Host '   ---------------------------------------------------------------'; break } } }"
    echo.
    echo   Fix what is shown above, then run quickstart.bat again.
    echo   It resumes from where it stopped - nothing is repeated unnecessarily.
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
echo.
pause
endlocal
