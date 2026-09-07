from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_bridge_keepalive_uses_a_nonblocking_single_supervisor_mutex():
    source = (ROOT / "scripts" / "start_bridge.ps1").read_text(encoding="utf-8")

    acquire = source.index("$keepaliveMutex.WaitOne(0)")
    first_edge_start = source.index("Ensure-Edge -Hard:$HardReset")
    assert "System.Threading.Mutex" in source
    assert "System.Threading.AbandonedMutexException" in source
    assert acquire < first_edge_start


def test_tunnel_ownership_timeout_is_indeterminate_and_never_auto_repaired():
    doctor = (ROOT / "scripts" / "doctor.ps1").read_text(encoding="utf-8")
    repair = (ROOT / "scripts" / "repair.ps1").read_text(encoding="utf-8")

    assert "Check-TriState \"tunnel_owned\"" in doctor
    assert "$attempt -le 3" in doctor
    assert "Invoke-DevTunnelBounded @('list') 10" in doctor
    assert "return $null" in doctor
    assert "indeterminate = $indeterminate" in doctor
    assert repair.count("-not $_.indeterminate") >= 3


# -- a fresh machine reported three red lines and could not be told apart from an idle one ----
#
# These are SOURCE assertions and cannot execute PowerShell; the Linux CI runner has no way to
# run doctor.ps1. The behaviour itself was verified by hand on Windows on 2026-09-07 by copying
# doctor.ps1, stubbing one probe per copy, and running each:
#   A. supervisor probe forced empty  -> [FAIL] Supervisor running + [SKIP] on the tunnel match
#   B. health probe forced false, err log seeded -> the reason printed inline under fix:
#   C. health probe forced false, both logs absent -> "produced no output to explain why"
#   D. supervisor.ps1's preservation block run over three simulated launches -> the live log
#      ends empty and all three crash reasons survive in the history file
# What is pinned below is that each defect cannot come back silently.

def test_a_supervisor_that_is_not_running_is_reported_rather_than_assumed():
    """THE DEFECT. The only supervisor-related check returned TRUE when no supervisor existed
    ("nothing to mismatch"), so it was green whether or not the stack had ever been started --
    and the server advice told the reader to use that green to tell those two cases apart."""
    doctor = (ROOT / "scripts" / "doctor.ps1").read_text(encoding="utf-8")

    assert 'Check "supervisor_running"' in doctor
    assert "if (-not $runCmdLine) { return $true }" not in doctor, "the fail-open green is back"
    # not applicable is reported as skipped, which is neither a pass nor a repairable failure
    assert 'Add-Result "tunnel_supervisor_match" $false' in doctor


def test_the_supervisor_check_is_probed_before_the_server_check_needs_the_answer():
    """The advice for a dead server branches on whether anything is relaunching it, so the
    probe has to have run by then. PowerShell executes top to bottom; ordering is the whole
    guarantee."""
    doctor = (ROOT / "scripts" / "doctor.ps1").read_text(encoding="utf-8")

    probe = doctor.index("$script:supervisorCmdLine = Get-RunningSupervisorCommandLineDoctor")
    server = doctor.index('Check "server_up"')
    assert probe < server


def test_doctor_reads_the_crash_log_instead_of_telling_the_user_to():
    """A diagnosis that depends on every user opening a log file is not one a product can ship.
    This failure was reported for a week with the log never read once -- and doctor knew the
    path the whole time."""
    doctor = (ROOT / "scripts" / "doctor.ps1").read_text(encoding="utf-8")

    assert "function Get-ServerLastOutput" in doctor
    assert "$script:serverFix" in doctor
    # the old advice handed over a path and a line count and left the reader to it
    assert "(last 20 lines). Read that" not in doctor
    # both places a reason can live are consulted, newest first
    assert "$script:serverErrLog" in doctor and "$script:serverErrHistory" in doctor


def test_the_crash_log_survives_the_relaunch_that_would_erase_it():
    """-RedirectStandardError TRUNCATES. The supervisor relaunches about once a minute while
    the server is failing, so the one file that explains the crash was being destroyed roughly
    sixty times an hour -- and an empty file reads as "no error"."""
    sup = (ROOT / "scripts" / "supervisor.ps1").read_text(encoding="utf-8")

    assert "server.err.history.log" in sup
    preserve = sup.index("Add-Content -Path $srvHist")
    launch = sup.index("-RedirectStandardOutput $srvOut -RedirectStandardError $srvErr")
    assert preserve < launch, "the previous launch is copied out AFTER it has been truncated"
    # bounded, or an unattended machine fills its disk with the same stack trace
    assert "-gt 262144" in sup and "-Tail 400" in sup


def test_doctor_resolves_the_interpreter_the_supervisor_would_actually_use():
    """The silent death had no name. supervisor.ps1 falls back from .venv to bare `python`,
    which on a fresh Windows machine is usually the Store App Execution Alias -- a stub that
    opens the Store and exits writing nothing, so the crash log cannot explain it. doctor must
    resolve it the SAME way or it answers a different question from the one that matters."""
    doctor = (ROOT / "scripts" / "doctor.ps1").read_text(encoding="utf-8")
    sup = (ROOT / "scripts" / "supervisor.ps1").read_text(encoding="utf-8")

    assert 'Check "python_runnable"' in doctor
    # same resolution order as the supervisor: .venv first, PATH second
    assert r".venv\Scripts\python.exe" in doctor and r".venv\Scripts\python.exe" in sup
    assert "WindowsApps" in doctor, "the one failure that leaves no trace is not named"
    # and the server line repeats it, rather than making the reader join two red lines
    assert "$script:pyProblem" in doctor
    assert doctor.index('Check "python_runnable"') < doctor.index('Check "server_up"')


def test_quickstart_falls_back_to_the_preserved_crash_log():
    """quickstart is the only surface most people touch, and its inline dump read the LIVE log
    only -- the one that every relaunch truncates. On the case the dump exists to cover it was
    routinely empty, so quickstart printed nothing while doctor, two lines above, printed the
    reason. Two instruments disagreeing about one failure is worse than one, because the reader
    believes the silent one."""
    qs = (ROOT / "quickstart.bat").read_text(encoding="utf-8")
    doctor = (ROOT / "scripts" / "doctor.ps1").read_text(encoding="utf-8")

    assert "server.err.history.log" in qs, "quickstart still reads only the truncated log"
    # same two files, same order, as doctor's own reader
    assert qs.index("server.err.log") < qs.index("server.err.history.log")
    assert doctor.index("$script:serverErrLog") < doctor.index("$script:serverErrHistory")
    # and quickstart still runs the doctor, or none of its checks reach anyone
    assert "scripts\doctor.ps1" in qs


def test_a_successful_startup_is_never_reported_as_the_reason_it_died():
    """MEASURED on this machine: a HEALTHY server's stderr ends with uvicorn's own
        INFO:     Application startup complete.
        INFO:     Uvicorn running on http://127.0.0.1:8000
    preceded by deprecation warnings. Reading "the last non-empty lines of stderr" as a cause of
    death therefore announces SUCCESS as a cause of death on any machine whose log survives a
    launch that worked -- worse than silence, because a reader given a false cause stops looking.
    Those lines still carry a true finding: the log is from a launch that came up, so it cannot
    explain a server that is down now."""
    doctor = (ROOT / "scripts" / "doctor.ps1").read_text(encoding="utf-8")
    qs = (ROOT / "quickstart.bat").read_text(encoding="utf-8")

    assert "Application startup complete" in doctor and "Uvicorn running on" in doctor
    # The two cases are separate branches on the marker, which is what makes the distinction --
    # not the wording, which is concatenated across source lines and cannot be matched here.
    # The rendered message was checked by running doctor with the health probe stubbed false
    # against this machine's real log, which ends in "Application startup complete": it says the
    # output on record is from a launch that started successfully and does not explain the
    # failure. A source assertion cannot see that; it can only see that the branch exists.
    assert "$script:STARTED_MARKERS" in doctor
    assert doctor.count("$out.started") >= 2, "the started/not-started cases are not split"
    # quickstart carries the same distinction, and only fires when the server is really down
    assert "Application startup complete" in qs
    assert "127.0.0.1:8000/health" in qs, "the block still fires on any red line"


def test_the_server_block_in_quickstart_is_gated_on_the_server_not_on_any_red_line():
    """It fired whenever doctor found ANY failure and stderr was non-empty, so an unrelated red
    -- a tunnel name, a missing agent URL -- printed "the MCP server tried to start and stopped"
    over a server that was running perfectly."""
    qs = (ROOT / "quickstart.bat").read_text(encoding="utf-8")
    block = qs[qs.index("$up = $false"):]
    assert block.index("Invoke-WebRequest") < block.index("server.err.log"), \
        "the log is read before the server is probed"


def test_the_first_server_launch_does_not_wait_out_the_debounce():
    """THE CAUSE OF THE THREE RED LINES, and it is not a fault at all.

    start_all.ps1 launches only the supervisor -- it never runs main.py itself -- and the
    supervisor counted FailuresBeforeAction (4) health checks at IntervalSeconds (15) apart
    before calling Start-Server even once. quickstart runs doctor as soon as start_all returns,
    so on every fresh machine the health check ran inside a 45-60 second window where the server
    did not yet exist BY DESIGN: server down, tunnel not serving, Bearer rejected -- one cause,
    no fault. It reproduced identically on two different weeks.
    """
    sup = (ROOT / "scripts" / "supervisor.ps1").read_text(encoding="utf-8")
    start_all = (ROOT / "scripts" / "start_all.ps1").read_text(encoding="utf-8")
    qs = (ROOT / "quickstart.bat").read_text(encoding="utf-8")

    # the counter starts primed, so the first failing check acts instead of counting to four
    # MEASURED, not assumed: this machine's supervisor log shows "supervisor up" at 11:44:39
    # and "MCP server process launched" at 11:45:52 -- 73 seconds -- and the ten runs before it
    # were all 65-75s.
    #
    # The condition is "nothing owns the port", NOT "the first check failed". Priming the
    # counter would let one transient /health timeout fire Start-Server against a server that
    # is perfectly healthy, and Start-Server kills whatever owns the port before relaunching.
    assert "Get-NetTCPConnection -LocalPort $Port -State Listen" in sup
    # A FAILED QUERY IS NOT AN EMPTY ANSWER. Catching the exception into $null made "could not
    # inspect the port" indistinguishable from "nothing owns it", and this branch calls
    # Start-Server, whose first act is to kill whatever owns the port. The debounce is the
    # correct behaviour when we cannot tell.
    assert "$portQueried = $true" in sup
    assert "if ($portQueried -and -not $portOwner)" in sup
    assert "if (-not $portOwner) {" not in sup, "a failed query licenses the kill again"
    assert "$serverMiss = $FailuresBeforeAction - 1" not in sup, "the unsafe form is back"
    assert sup.index("nothing is listening on :$Port at startup") \
        < sup.index("$serverMiss -ge $FailuresBeforeAction"), \
        "the cold-start launch sits inside the debounce branch"

    # the premise: nothing but the supervisor starts the server
    assert "supervisor.ps1" in start_all
    assert 'ArgumentList "main.py"' in sup

    # and quickstart waits for the server before asking whether it is healthy
    wait = qs.index("Waiting for the MCP server to answer")
    doctor_call = qs.index("scripts\doctor.ps1")
    assert wait < doctor_call, "the health check still runs before the wait"


def test_every_devtunnel_resolver_knows_both_install_locations():
    """setup_devtunnel.ps1 installs by winget when it can and DIRECT-DOWNLOADS to
    %LOCALAPPDATA%\devtunnel when it cannot, appending that directory to the USER PATH -- which
    the already-running cmd cannot see, and that cmd is quickstart, the parent of start_all,
    supervisor and doctor. So on exactly the locked-down machines that needed the fallback, the
    CLI was installed and unfindable, and doctor advised installing what was already there.

    All four resolvers, not the two that happened to be noticed: this is one failure class."""
    installer = (ROOT / "scripts" / "setup_devtunnel.ps1").read_text(encoding="utf-8")
    assert 'Join-Path $env:LOCALAPPDATA "devtunnel"' in installer, "the install location moved"

    for rel in ("scripts/doctor.ps1", "scripts/heal_tunnel.ps1", "scripts/supervisor.ps1"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert 'Join-Path $env:LOCALAPPDATA "devtunnel\devtunnel.exe"' in src, rel
    boot = (ROOT / "scripts" / "bootstrap.py").read_text(encoding="utf-8")
    assert 'local / "devtunnel" / "devtunnel.exe"' in boot


def test_the_supervisor_refuses_rather_than_looping_on_an_interpreter_that_cannot_run():
    """It fell back from .venv to bare `python`, which on a fresh machine is usually the App
    Execution Alias under WindowsApps -- not an interpreter; run with arguments it returns an
    error code. The loop would fail identically twice a pass, every fifteen seconds, forever,
    while reporting itself as running -- which is what doctor shows, in green."""
    sup = (ROOT / "scripts" / "supervisor.ps1").read_text(encoding="utf-8")

    assert "REFUSING TO RUN" in sup
    assert "exit 3" in sup
    # Built from a backslash constant, not typed out: the previous version of this line
    # asked for one backslash where the regex needs two (an escaped backslash matches a
    # literal one), because the heredoc that wrote the test de-escaped it. Counting
    # backslashes by eye through three layers of quoting is how that goes wrong.
    bs = chr(92)
    assert ("-match '" + bs * 2 + "WindowsApps" + bs * 2 + "'") in sup, \
        "the Store alias is not detected"
    # the refusal happens before the loop can start using it
    assert sup.index("REFUSING TO RUN") < sup.index("$serverMiss -ge $FailuresBeforeAction")
    # and the old unconditional fallback is gone
    assert 'if (-not (Test-Path $Py)) { $Py = "python" }' not in sup


def test_the_supervisors_own_startup_output_is_captured_and_read():
    """d15a834 closed this hole for the server and left it open on the thing that launches it.
    A supervisor that dies during startup -- Constrained Language Mode refusing New-Object
    Mutex, AppLocker blocking the script, its own "REFUSING TO RUN: no usable Python" -- wrote
    its reason into a hidden window that discards it, and doctor then advised double-clicking
    start_all.bat, which is the operation that just ran.

    Capturing it is half the fix; the reader is the other half. The server's log went unread for
    a week when only the capture existed."""
    start_all = (ROOT / "scripts" / "start_all.ps1").read_text(encoding="utf-8")
    doctor = (ROOT / "scripts" / "doctor.ps1").read_text(encoding="utf-8")

    assert "supervisor.err.log" in start_all, "the supervisor's startup output is discarded"
    assert "-RedirectStandardError $supErr" in start_all
    # a log that cannot be opened must not keep the stack down
    assert start_all.count("Start-Process powershell -WindowStyle Hidden -ArgumentList $supArgs") == 2

    assert "supervisor.err.log" in doctor, "nobody reads it"
    assert "was started and STOPPED" in doctor, "the two causes are not separated"


def test_quickstart_reads_the_exit_codes_those_scripts_go_to_the_trouble_of_returning():
    """configure_env.ps1 returns 2 (cancelled), 3 (saved but the agent URL is still blank) and
    4 (the dialog could not run); its own comment calls 3 "A DISTINCT CODE. The caller can tell
    ... from ...", and the caller was not looking. register-supervisor.ps1 returns 1 when it
    cannot write the Startup entry -- the whole point of the step the user just agreed to --
    and that surfaced only at the next logon, as the stack not being there."""
    qs = (ROOT / "quickstart.bat").read_text(encoding="utf-8")
    cfg = (ROOT / "scripts" / "configure_env.ps1").read_text(encoding="utf-8")
    reg = (ROOT / "scripts" / "register-supervisor.ps1").read_text(encoding="utf-8")

    for code in ("exit 2", "exit 3", "exit 4"):
        assert code in cfg, "configure_env no longer returns %s" % code
    assert "exit 1" in reg

    after_cfg = qs[qs.index("configure_env.ps1"):]
    for n in (4, 3, 2):
        assert "if errorlevel %d" % n in after_cfg, "code %d is still discarded" % n
    # highest first: `if errorlevel N` means "N or greater", so 2 first would swallow 3 and 4
    assert (after_cfg.index("if errorlevel 4") < after_cfg.index("if errorlevel 3")
            < after_cfg.index("if errorlevel 2"))

    after_reg = qs[qs.index("register-supervisor.ps1"):]
    assert "if errorlevel 1" in after_reg


def test_doctor_asks_whether_the_unlock_password_can_be_read_here():
    """THE FAULT THAT LEAVES EVERYTHING GREEN. MCP_UNLOCK_PASSWORD_PROTECTED is DPAPI, bound to
    one Windows account on one machine, so an .env carried from another PC holds a blob this
    account cannot open. The server starts, the Bearer check passes, doctor reports ALL GREEN --
    and every write, run_python and shell call is refused. This project has already lost days to
    it. env_portability.problems() was written to report exactly this and had no caller outside
    its own tests.

    Verified to go RED, not merely to exist: problems() was run against an .env holding an
    undecryptable blob (FAIL) and against a locally-set password (PASS)."""
    doctor = (ROOT / "scripts" / "doctor.ps1").read_text(encoding="utf-8")

    assert 'Check-TriState "unlock_password_usable"' in doctor, \
        "a check that cannot ask must not be able to report PASS"
    # THE LOGIC IS A FILE, NOT A `-c` PAYLOAD. Measured: Start-Process -ArgumentList @("-c",
    # $code) does not quote the element, python received only the first word and answered
    # SyntaxError, the non-zero exit was read as "could not ask", and "could not ask" was
    # converted to PASS -- so the first version of this check could only ever be green. Both
    # sides had been tested; the seam between them had not.
    assert (ROOT / "scripts" / "check_unlock_usable.py").exists()
    assert "check_unlock_usable.py" in doctor
    assert "function Invoke-BoundedPythonFile" in doctor
    # COMMENTS OUT FIRST. The line above explains the removed form by quoting it, and an
    # assertion that reads prose as code fails on its own explanation -- a mistake this repo
    # has already made once.
    doctor_code = "\n".join(l for l in doctor.splitlines() if not l.lstrip().startswith("#"))
    assert '@("-c", $code)' not in doctor_code, "the payload form that could not survive is back"
    # every argument quoted, so a path with a space is not two arguments
    assert "'\"{0}\"' -f $script" in doctor
    # bounded: doctor must not hang because python did
    assert "$p.Kill()" in doctor
    # and the checker itself distinguishes the three states, unset included
    checker = (ROOT / "scripts" / "check_unlock_usable.py").read_text(encoding="utf-8")
    for verdict in ("undecryptable", "unset", "ok"):
        assert ('print("%s")' % verdict) in checker or ("'%s'" % verdict) in checker, verdict


def test_a_failed_unlock_repair_is_reported_not_only_a_successful_one():
    """repair_unlock_password returns "cannot protect a new value here: ...", "refusing to edit
    without a backup: ..." and "cannot read ...". Matching only "re-established" made every one
    of those land as silence -- so a repair that FAILED looked exactly like a machine that never
    needed one, and the symptom arrives hours later as every mutating tool refused."""
    start_all = (ROOT / "scripts" / "start_all.ps1").read_text(encoding="utf-8")
    ep = (ROOT / "tools" / "env_portability.py").read_text(encoding="utf-8")

    # The repair now runs scripts/repair_unlock.py, which prints one of
    # noop:<why> / repaired:<password> / failed:<why> -- a script file rather than a `-c`
    # payload, for the reason the unlock CHECK had to become one.
    assert 'repair -like "failed:*"' in start_all
    assert "UNLOCK PASSWORD REPAIR FAILED" in start_all
    # the reasons really do start with those words
    assert '"reason": "cannot protect a new value here' in ep or "cannot protect a new value here" in ep
    assert "refusing to edit without a backup" in ep


def test_the_server_is_up_before_step_5_asks_for_a_connection_test():
    """copilot_studio_values.ps1 tells the operator "Add connection / Test (the tool list should
    load)", and the server was not launched until STEP 7 -- so the first thing that happened on
    the only manual step of the install was a connection error. The 90-second wait added earlier
    sits after STEP 7 and cannot help."""
    qs = (ROOT / "quickstart.bat").read_text(encoding="utf-8")
    start_all = (ROOT / "scripts" / "start_all.ps1").read_text(encoding="utf-8")
    values = (ROOT / "scripts" / "copilot_studio_values.ps1").read_text(encoding="utf-8")

    assert "Add connection" in values, "STEP 5 no longer asks for a connection test"
    assert "[switch]$CoreOnly" in start_all
    # the gate demands the agent URL, which is what STEP 5 exists to create -- asking would deadlock
    assert "first-time interactive setup skipped (-CoreOnly)" in start_all
    core = qs.index("-CoreOnly")
    step5 = qs.index("STEP 5/7")
    full = qs.index("STEP 7/7")
    assert core < step5 < full, "the core start is not between the tunnel and the manual step"


def test_the_tunnel_access_decision_is_asked_recorded_and_applied():
    """MEASURED on the machine that works: `devtunnel access list` shows +Anonymous [connect]
    while MCP_TUNNEL_ALLOW_ANONYMOUS is absent from its .env -- so the working configuration was
    granted some other way, and a new machine following the default gets a tunnel with NO grant.
    Nothing can connect to it, and STEP 5's test fails.

    Anonymous exposure is not switched on quietly: setup_devtunnel's own comment says it "must
    be a deliberate choice by the operator, not a silent default". So it is asked, the answer is
    recorded the way the convenience block records its own, and whichever was chosen is applied.
    The tenant option used to be a command printed for the operator to type."""
    qs = (ROOT / "quickstart.bat").read_text(encoding="utf-8")
    dt = (ROOT / "scripts" / "setup_devtunnel.ps1").read_text(encoding="utf-8")

    assert "choice /C ATN" in qs
    assert 'tunnel_access_choice' in qs, "the decision is not recorded"
    # asked BEFORE the tunnel is created, because the grant is part of creating it
    assert qs.index("choice /C ATN") < qs.index("STEP 4/7")
    # default stays off: the env line is only written on an explicit A
    assert 'if "!TUNNEL_ACCESS!"=="anonymous" (' in qs
    # and tenant access is applied, not printed
    assert "[string]$TenantId" in dt
    assert "access create $target --tenant $TenantId" in dt
    # `set /p` is not inside a parenthesized block: measured, it did not settle before the `if`
    assert "goto :after_tenant_id" in qs


def test_a_fresh_browser_with_no_tab_leads_to_a_sign_in():
    """start_companion_edge.ps1 opens at about:blank, so a fresh machine has no M365 tab.
    state() answers None for that AND for "Edge is not answering", and the setup path treated
    both as "the companion Edge is not running" -- returning 2, which the wrapper turns into 0
    and doctor logs as INFO. quickstart therefore finished reporting success with nobody signed
    in. --check-only is untouched: with no tab it still answers "cannot tell", because the fleet
    is websocket-driven and a signed-in machine shows no tabs either."""
    signin = (ROOT / "scripts" / "ensure_m365_signin.py").read_text(encoding="utf-8")

    assert 'if ready is None and tabs(a.port) is None:' in signin, \
        "the two reasons for None are conflated again"
    assert "no M365 page is open yet" in signin
    # the check-only path still declines to judge
    assert "return 1 if ready is False else 2" in signin


def test_the_new_unlock_password_reaches_the_operator():
    """repair_unlock_password generates a NEW random password -- correctly, the old one is
    unreadable on this account -- and returned only a reason and a backup path. The operator
    arrived with a password written down from the machine that produced the .env, and nothing
    ever told them it no longer works. Everything green; unlock() simply refuses."""
    ep = (ROOT / "tools" / "env_portability.py").read_text(encoding="utf-8")
    start_all = (ROOT / "scripts" / "start_all.ps1").read_text(encoding="utf-8")

    assert '"password": fresh' in ep
    assert (ROOT / "scripts" / "repair_unlock.py").exists()
    assert "repair_unlock.py" in start_all
    assert 'repair -like "repaired:*"' in start_all
    assert "Write this down" in start_all


def test_a_required_check_that_could_not_be_answered_is_not_a_complete_setup():
    """$script:warn holds two different things: an OPTIONAL component being absent, which is a
    complete setup, and a REQUIRED check that could not be determined, which is not. doctor
    exits with $script:bad, so neither reached quickstart and the banner said COMPLETE over a
    run where something required was never established.

    I introduced a case of this today: making the unlock check Check-TriState stopped "could not
    ask" being reported as a PASS -- correct -- but the indeterminate it produces lands in warn,
    and the install still declared itself complete. Fixing the inner fail-open had moved the
    problem outward rather than removing it.

    Check-TriState is used only for required checks, so counting its indeterminates separately
    is exact. Verified by running the whole quickstart flow with every external call stubbed:
    unknown=2 prints NOT CONFIRMED, unknown=0 prints COMPLETE."""
    doctor = (ROOT / "scripts" / "doctor.ps1").read_text(encoding="utf-8")
    qs = (ROOT / "quickstart.bat").read_text(encoding="utf-8")

    assert "$script:unknown = 0" in doctor
    assert "$script:unknown++" in doctor
    assert "doctor_summary.txt" in doctor, "the counts are not published anywhere"
    # the exit code keeps meaning "number of failures": repair.ps1 parses -Json, and quickstart
    # prints "N check(s) failed" from it
    assert "exit $script:bad" in doctor
    assert "SETUP NOT CONFIRMED" in qs
    # COMMENTS OUT FIRST -- the third time today an assertion matched the prose explaining
    # a thing rather than the thing. A REM line above the banner quotes "SETUP COMPLETE".
    qs_code = "\n".join(l for l in qs.splitlines()
                        if not l.strip().lower().startswith("rem"))
    assert qs_code.index("SETUP NOT CONFIRMED") < qs_code.index("SETUP COMPLETE"), \
        "the complete banner is reached before the unanswered case is considered"


def test_the_update_step_reads_its_result_and_stops_after_replacing_itself():
    """Two problems, and the second is the dangerous one. `git pull --ff-only`'s result was not
    read, so a failed pull left the operator believing they were current while running the old
    code. And a SUCCESSFUL pull rewrites quickstart.bat while cmd is executing it -- cmd resumes
    a batch file from a byte offset after each line, so replacing it underneath a running
    instance continues at whatever now occupies that offset. Undefined, and silent.

    No labels: the first attempt put goto targets inside the parenthesised block and cmd
    rejected the entire file with ") was unexpected at this time". Measured, both of them."""
    qs = (ROOT / "quickstart.bat").read_text(encoding="utf-8")

    assert 'if defined DO_PULL (' in qs
    assert "UPDATE FAILED" in qs
    assert "Updated. Please run quickstart.bat again." in qs
    # the decision is taken inside the block, acted on outside it
    assert qs.index('set "DO_PULL=1"') < qs.index("if defined DO_PULL (")
    # and no label was reintroduced into the git block
    git_block = qs[qs.index("STEP 3/7"):qs.index("STEP 4/7")]
    assert ":do_pull" not in git_block, "a label is back inside a parenthesised block"


def test_an_unanswerable_access_prompt_records_the_safe_answer():
    """MEASURED: with stdin closed, `choice` prints "ERROR: The file is either empty or does not
    contain the valid choices" and sets none of the branches, leaving the variable empty. The
    effect was already safe -- nothing is granted -- but nothing said so, and the recorded
    decision was a blank. An absent answer is the same answer as N and is written down as one."""
    qs = (ROOT / "quickstart.bat").read_text(encoding="utf-8")
    assert 'if "!TUNNEL_ACCESS!"=="" set "TUNNEL_ACCESS=none"' in qs


def test_start_all_reports_its_failure_count_and_quickstart_reads_it():
    """It already collected $script:startupFailures and printed them last, deliberately, so they
    would not scroll past -- and then exited 0 regardless. A startup that failed and one that
    worked were indistinguishable to the caller, which is why quickstart went on to the manual
    Copilot Studio step after a launch that had not happened.

    The count is the code, the convention doctor already uses. Nothing read it before
    (start_all.bat runs the VBS without checking; the Desktop launcher and logon task do not
    look), so giving it meaning cannot break what works. Both quickstart launches were exercised
    against a stub returning 0 and 2."""
    start_all = (ROOT / "scripts" / "start_all.ps1").read_text(encoding="utf-8")
    qs = (ROOT / "quickstart.bat").read_text(encoding="utf-8")

    assert "exit $script:startupFailures.Count" in start_all
    # both launches read it -- the core one because STEP 5's connection test depends on it
    assert "The core startup reported problem" in qs
    assert 'set "START_ALL_BAD=%ERRORLEVEL%"' in qs
    # captured immediately, before any command can reset it
    tail = qs[qs.index('set "START_ALL_BAD=%ERRORLEVEL%"') - 200:]
    assert tail.index("start_all.ps1") < tail.index('set "START_ALL_BAD=%ERRORLEVEL%"')
    # neither stops the run: the health check at the end reports either way
    assert "exit /b" not in qs[qs.index("The core startup reported problem"):
                               qs.index("STEP 5/7")]


def test_a_blocked_setup_dialog_asks_in_the_console_instead_of_dead_ending():
    """configure_env.ps1 exits 4 when Add-Type/WinForms is refused -- its own comment names
    AppLocker on managed machines, which is the kind of machine this gets installed on. The only
    way out was the printed advice "edit .env by hand", which an installer cannot rely on, and
    the one REQUIRED value stayed unset.

    cmd can ask for a line of text on any machine. Only the required key: the fleet URL is
    optional and falls back to the main one. Exercised with configure_env stubbed to 4 and the
    agent URL absent -- the fallback ran and reached the .env write."""
    qs = (ROOT / "quickstart.bat").read_text(encoding="utf-8")
    cfg = (ROOT / "scripts" / "configure_env.ps1").read_text(encoding="utf-8")

    assert "exit 4" in cfg, "configure_env no longer reports a blocked dialog"
    assert "CFG_DIALOG_BLOCKED" in qs
    assert "MCP_IMPL_AGENT_URL: " in qs, "the console fallback does not ask"
    assert "by hand, then run quickstart.bat again." not in qs, "the dead-end advice is back"
    # only asks when the key is still absent, so a re-run does not append a duplicate
    fb = qs[qs.index("CONSOLE FALLBACK"):]
    assert fb.index('findstr /b /r "MCP_IMPL_AGENT_URL=..*"') < fb.index("set /p IMPL_URL")
    # flattened: set /p inside a block did not settle before the if that read it
    assert "goto :after_cfg_fallback" in qs


def test_auth_ok_end_to_end_means_more_than_not_401():
    """The last check in the run, the one whose name promises the whole path works, passed on
    any status that was not 401/403/0 -- so 404, 405 and 500 all read as "Auth OK end-to-end".
    And a server with authentication switched off passed too, because nothing asked what
    happens WITHOUT the key, which is the only observation that says anything about auth.

    MEASURED against the live server: correct key -> 400 (the probe body is not a full MCP
    handshake; auth ran first and passed), no key -> 401, wrong key -> 401, bogus path -> 404.
    So requiring 200 would be wrong. Every failing mode was then exercised through the real
    doctor with Mcp-Status stubbed: 404, 500, an unenforced 400/400, a rejected key and a dead
    server all go red; only 400/401 stays green. 5xx passed the first version of this fix --
    excluding codes one at a time left server errors in -- which is why it is a range now."""
    doctor = (ROOT / "scripts" / "doctor.ps1").read_text(encoding="utf-8")

    auth = doctor[doctor.index('Check "auth_bearer"'):]
    auth = auth[:auth.index("Write-Host")]
    assert "$noKey = Mcp-Status @{}" in auth, "nothing checks that a missing key is refused"
    assert "($noKey -eq 401) -or ($noKey -eq 403)" in auth
    assert "$withKey -ge 200" in auth and "$withKey -lt 500" in auth, "5xx passes again"
    assert "$withKey -ne 404" in auth


def test_the_chat_backend_is_checked_not_just_the_browser_it_drives():
    """The only bridge check probed the Edge CDP port and was marked optional -- "only needed
    for past-conversation history". The thing CopilotChat actually talks to is the HTTP server
    on :8765, and nothing looked at it.

    OBSERVED on the machine that was supposed to be working, while writing this: / answered 200
    while /conv dropped the connection, and :8765 was held by a process started with the SYSTEM
    python rather than the venv's -- so it could not serve. start_all uses /conv as its liveness
    probe, decided the bridge was down, started another, and the wedged one kept the port. Four
    bridge processes, five and a half hours, behind a green health check.

    /conv and not /, precisely because /conv is what start_all trusts: probing / would have
    passed there and called a broken chat backend fine."""
    doctor = (ROOT / "scripts" / "doctor.ps1").read_text(encoding="utf-8")
    start_all = (ROOT / "scripts" / "start_all.ps1").read_text(encoding="utf-8")

    assert 'Check "bridge_backend"' in doctor
    assert "127.0.0.1:8765/conv" in doctor
    # the same endpoint start_all decides on, or the two instruments can disagree
    assert "127.0.0.1:8765/conv" in start_all
    # required: a chat backend that does not serve is not a complete setup
    block = doctor[doctor.index('Check "bridge_backend"'):]
    block = block[:block.index("# 5b.")]
    assert "-Optional" not in block
    # an HTTP error still means something is serving; a dropped connection does not
    assert "else { $false }" in block


def test_a_held_but_dead_bridge_port_is_named_rather_than_relaunched_into():
    """/conv is the liveness probe, and a bridge started outside the venv answers / but not
    /conv -- so this branch concluded "down", launched another that could not bind the port, and
    repeated. Seen running for five and a half hours with four bridge processes alive, each pass
    printing "[3/4] bridge: starting" as though it had worked.

    Named, not killed: the owner is a process this stack did not start, and the rule here is not
    to touch those. Recorded as a startup failure instead, which the exit code now carries.
    Verified against the live fault: it names pid and command line."""
    start_all = (ROOT / "scripts" / "start_all.ps1").read_text(encoding="utf-8")

    assert "Get-NetTCPConnection -LocalPort 8765" in start_all
    assert "is HELD by pid" in start_all
    # not killed
    block = start_all[start_all.index("STARTING ANOTHER CANNOT HELP"):]
    block = block[:block.index("bridge: starting (headless keepalive)")]
    assert "Stop-Process" not in block, "it kills a process this stack did not start"
    # and it counts, so the exit code and quickstart see it
    assert '$script:startupFailures += "bridge: :8765 held by pid' in start_all


def test_a_keepalive_process_is_not_a_serving_bridge():
    """THE ROOT OF THE FIVE-AND-A-HALF-HOUR LOOP. The first branch asked only whether the
    start_bridge.ps1 wrapper existed, so a wedged python holding :8765 behind a live keepalive
    reported "already running" and nothing looked further -- the port-owner diagnosis below it
    was never even reached. Process existence standing in for liveness, which is the same shape
    as the supervisor check that was green whether or not a supervisor was running."""
    start_all = (ROOT / "scripts" / "start_all.ps1").read_text(encoding="utf-8")

    assert "(Proc-Running 'start_bridge\.ps1') -and (Http-Up" in start_all
    assert "already running and serving" in start_all
    # and the diagnosis below is now reachable when it is running but not serving
    assert start_all.index("already running and serving") < start_all.index("is HELD by pid")


def test_the_supervisor_probe_matches_this_checkout_and_not_whoever_mentions_it():
    """MEASURED while writing this. `CommandLine -match 'supervisor\.ps1'` selected five
    processes on this machine: the real supervisor, another powershell, and three bash commands
    that contained the string because they were SEARCHING for it. This project already has the
    lesson written down -- a process query matches the process making it.

    doctor took -First 1, so "Supervisor running" could go green on a shell command. Worse,
    start_all's drift restart STOPS every match: on a two-checkout machine that kills the other
    one's supervisor, and here it would have killed a shell.

    Scoped by this checkout's resolved script path, requiring a PowerShell host, excluding the
    register/unregister scripts that share the name. -like rather than -match, so a path of
    backslashes needs no escaping -- a count that has already gone wrong once today."""
    doctor = (ROOT / "scripts" / "doctor.ps1").read_text(encoding="utf-8")
    start_all = (ROOT / "scripts" / "start_all.ps1").read_text(encoding="utf-8")

    for src, what in ((doctor, "doctor"), (start_all, "start_all")):
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        assert "$_.CommandLine -match 'supervisor\.ps1'" not in code, \
            "%s matches anything that mentions the file again" % what
        assert '$_.Name -match \'^(powershell|pwsh)\'' in code, what
        assert '-notlike "*register-supervisor*"' in code, what
        assert 'Join-Path $scriptDir "supervisor.ps1"' in code, what


def test_no_tunnel_name_is_not_ownership():
    """Test-TunnelOwned returned $true for an empty name, so a machine with no MCP_TUNNEL_NAME
    at all reported "Dev Tunnel name is owned by this account". And this check is not part of
    the tunnel chain, so it is not skipped when the name is missing -- it simply went green.

    $null is the contract the function already had for "could not be determined", which is what
    this is. Verified: with the name stubbed empty the line reads [WARN] indeterminate rather
    than [ OK ], and that now counts toward the unanswered-required total."""
    doctor = (ROOT / "scripts" / "doctor.ps1").read_text(encoding="utf-8")

    owned = doctor[doctor.index("function Test-TunnelOwned"):]
    owned = owned[:owned.index("\n}\n")]
    assert "IsNullOrWhiteSpace($name)) { return $null }" in owned, \
        "an unset tunnel name reads as owned again"
    assert "{ return $true }" not in owned.split("for ($attempt")[0]


def test_the_cdp_checks_read_the_answer_instead_of_discarding_it():
    """`Get-Json '...' | Out-Null; $true` passed on anything that answered that URL with any
    JSON -- another browser, a dev server, a proxy -- while the check's name claims a browser.
    The shape does fail closed on a dead port (measured: a request to a free port throws and the
    check goes red), so this was not the green-while-broken case the others were; it was a check
    confirming "something is there" under a name that says which something.

    What the real endpoint returns, measured: Browser="Edg/152...", plus a webSocketDebuggerUrl
    that makes it CDP rather than a document that happens to be JSON. Exercised against a live
    Edge (true), a JSON impostor on another port (false) and a free port (false)."""
    doctor = (ROOT / "scripts" / "doctor.ps1").read_text(encoding="utf-8")

    assert "function Test-EdgeCdp" in doctor
    assert '$v.Browser) -like "Edg*"' in doctor
    assert '$v.webSocketDebuggerUrl) -like "ws://*"' in doctor
    code = "\n".join(l for l in doctor.splitlines() if not l.lstrip().startswith("#"))
    assert "json/version' | Out-Null; $true" not in code, "the discarded-answer form is back"


def test_the_exit_code_has_something_to_count():
    """Making $script:startupFailures.Count the exit code was only half a fix: the array was
    fed by exactly two things -- an exception escaping Invoke-Startup entirely, and the bridge
    port case added alongside it. Every individual component failure was printed and not
    counted, so the code the caller now reads was almost always 0 regardless.

    The instrument existing while nothing feeds it is the same shape as everything else found
    today. These two are failures the code already KNOWS about: an unlock repair that failed
    means every mutating tool is refused, and a UI that did not build means the windows the
    setup promises will not open."""
    start_all = (ROOT / "scripts" / "start_all.ps1").read_text(encoding="utf-8")

    assert "exit $script:startupFailures.Count" in start_all
    assert '$script:startupFailures += ("unlock password repair failed: ' in start_all
    assert '$script:startupFailures += "${app}: rebuild failed"' in start_all
    # each recording sits with the message that already reported it
    assert start_all.index("UNLOCK PASSWORD REPAIR FAILED") < \
        start_all.index('$script:startupFailures += ("unlock password repair failed: ')


def test_appending_to_env_cannot_join_the_new_key_onto_the_last_one():
    """MEASURED, all three forms:

        echo KEY=1 >> f                          -> "KEY=1 \r\n"   the space lands in the VALUE
        >> f echo KEY=1                          -> "KEY=1\r\n"    the form that was in use
        >> f echo KEY=1  (no trailing newline)   -> "A=1KEY=1\r\n"  THE KEYS ARE JOINED

    So the trailing-space fault does not apply here -- the redirection-first form was already
    right about that, and claiming otherwise would be repeating a guess as a finding. The join
    does apply, and it destroys the key that was already there: if that is MCP_API_KEY, the
    Bearer token is silently wrong and nothing works.

    Both appends were added today, both by me."""
    qs = (ROOT / "quickstart.bat").read_text(encoding="utf-8")

    code = "\n".join(l for l in qs.splitlines() if not l.strip().lower().startswith("rem"))
    assert '>> ".env" echo' not in code, "an append that can join lines is back"
    assert code.count("[IO.File]::AppendAllText") >= 2
    assert "$b[$b.Length-1] -ne 10" in code, "nothing checks for the trailing newline"


def test_the_access_choice_beats_the_file_and_the_environment():
    """Choosing A did not reliably grant anonymous access. Get-AllowAnonymous reads the parent
    environment FIRST and then scans .env taking the FIRST match and breaking -- so appending
    the key at the end had no effect whenever the variable was set in the environment, or .env
    already carried the key with another value further up. The operator was told "Recorded:
    anonymous access" and the tunnel was created without the grant.

    Two changes, because either alone is insufficient: the answer is passed as -ForceAnonymous
    so THIS run does what was just chosen, and the key is REPLACED in .env so the choice
    survives to later runs. Verified against a .env already carrying =0: one line remains, it
    says 1, and the other keys are untouched."""
    qs = (ROOT / "quickstart.bat").read_text(encoding="utf-8")
    dt = (ROOT / "scripts" / "setup_devtunnel.ps1").read_text(encoding="utf-8")

    assert "[switch]$ForceAnonymous" in dt
    assert "$AllowAnonymous = $ForceAnonymous.IsPresent -or (Get-AllowAnonymous)" in dt
    assert "-ForceAnonymous" in qs and "ANON_FLAG" in qs
    # replaced, not appended: an older line further up would otherwise keep winning
    assert "MCP_TUNNEL_ALLOW_ANONYMOUS\s*=" in qs
    assert "Set-Content -Path $p -Value $keep" in qs


def test_the_self_restart_keeps_every_switch_it_was_given():
    """start_all re-executes itself after an update and rebuilt only -NoUi and -NoSplash. A
    restart during the core start would come back as a FULL start, walk into
    Invoke-FirstTimeSetupGate and demand the agent URL -- which is exactly what STEP 5 has not
    created yet, since -CoreOnly exists to run before STEP 5. Its own path was unquoted too."""
    start_all = (ROOT / "scripts" / "start_all.ps1").read_text(encoding="utf-8")

    block = start_all[start_all.index("$reArgs = @("):]
    block = block[:block.index("Start-Process")]
    assert 'if ($CoreOnly) { $reArgs += "-CoreOnly" }' in block
    assert "'\"{0}\"' -f $selfPath" in block, "the script path is unquoted again"


def test_the_daily_launcher_checks_the_m365_session():
    """ensure_m365_signin was called from quickstart once, at install time, and by doctor with
    --check-only as an [INFO] that counts toward nothing. start_all -- the Desktop shortcut, the
    logon task, everything that runs day to day -- did not call it at all. M365 sessions expire,
    and when one does the stack comes up entirely green while the agent silently cannot work.

    Handing the operator a script to run by hand is not the fix; the launcher carries it. A
    background start asks without surfacing a browser at nobody, a manual start brings the
    window forward, bounded at 180s rather than the helper's 600s default.

    THE PYTHON DIRECTLY in the background branch, because the wrapper ends with an unconditional
    `exit 0` -- deliberately, so a missing sign-in never fails the whole setup -- which would
    have made a check of its exit code dead code. Caught before committing this time. Measured:
    no M365 tab and no browser both give 2 ("could not tell"), which is correctly ignored,
    because the fleet opens no tabs and a signed-in machine looks the same."""
    start_all = (ROOT / "scripts" / "start_all.ps1").read_text(encoding="utf-8")
    wrapper = (ROOT / "scripts" / "ensure_m365_signin.ps1").read_text(encoding="utf-8")

    assert "ensure_m365_signin" in start_all, "the daily launcher still does not look"
    assert "[switch]$CheckOnly" in wrapper
    # the wrapper still refuses to fail the setup, which is why the code is read elsewhere
    assert "exit 0" in wrapper
    block = start_all[start_all.index("THE SESSION EXPIRES AND NOTHING LOOKED"):]
    block = block[:block.index("WHERE TO LOOK")]
    assert "ensure_m365_signin.py" in block, "the background branch calls the wrapper again"
    assert "$LASTEXITCODE -eq 1" in block
    assert "-eq 2" not in block, "could-not-tell must not be reported as a fault"
    assert "-TimeoutSeconds 180" in block


def test_the_skip_test_asks_about_the_key_it_actually_needs():
    """`findstr /r` treats a space as OR. Measured against a .env holding only one of the two:

        IMPL only  -> exit 0   skip STEP 5/6
        FLEET only -> exit 0   skip STEP 5/6   <- and IMPL is still empty
        neither    -> exit 1   do STEP 5/6

    So a machine carrying just the fleet URL skipped the step that sets the REQUIRED key and
    reported "already configured". The fleet URL is optional and falls back to IMPL, and the
    console fallback three lines below already tested IMPL alone -- the two disagreed."""
    qs = (ROOT / "quickstart.bat").read_text(encoding="utf-8")

    code = "\n".join(l for l in qs.splitlines() if not l.strip().lower().startswith("rem"))
    assert 'MCP_IMPL_AGENT_URL=..* MCP_FLEET_AGENT_URL=..*' not in code, "the OR pattern is back"
    assert 'findstr /b /r "MCP_IMPL_AGENT_URL=..*" ".env"' in code


def test_the_repair_verdict_is_chosen_by_prefix_not_by_position():
    """repair_unlock.py puts one verdict line on stdout, and the caller took the LAST line of a
    2>&1 merged stream. Measured with a warning arriving after the verdict:

        by position -> "DeprecationWarning: something"
        by prefix   -> "noop:the unlock password is readable"

    A mismatched verdict matches neither "repaired:*" nor "failed:*", so the new password is
    never shown AND the failure is never recorded. This machine emits nothing on stderr, which
    is the only reason it worked here. My code, from today, and the same class -- taking an
    answer and dropping it -- this round of work exists to remove."""
    start_all = (ROOT / "scripts" / "start_all.ps1").read_text(encoding="utf-8")

    assert "Select-Object -Last 1)" not in start_all.split("repairScript")[1][:200], \
        "the verdict is picked by position again"
    assert "'^(noop|repaired|failed|error):'" in start_all


def test_a_supervisor_that_dies_immediately_is_noticed():
    """Start-Process fire-and-forget: no -PassThru, no exit code. supervisor.ps1 exits 3 when
    there is no usable Python -- added today -- and start_all printed "[1/4] supervisor:
    starting" and moved on regardless, asymmetric with the unlock repair, the bridge port and
    the UI rebuild, which are all counted. A supervisor that died is the difference between a
    stack that comes up in ninety seconds and one that never comes up at all."""
    start_all = (ROOT / "scripts" / "start_all.ps1").read_text(encoding="utf-8")

    assert "-RedirectStandardError $supErr -PassThru" in start_all
    assert "$supProc.HasExited" in start_all
    assert '$script:startupFailures += $why' in start_all
    # it refuses within its first statements, so a short wait separates death from running
    assert "$supProc.WaitForExit(3000)" in start_all
