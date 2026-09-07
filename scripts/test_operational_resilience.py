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
    assert "if (-not $portOwner)" in sup
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
