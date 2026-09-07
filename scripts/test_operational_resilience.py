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
