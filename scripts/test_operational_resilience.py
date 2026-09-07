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

    assert "function Get-ServerDeathReason" in doctor
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
