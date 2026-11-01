"""Scenario tests for the stale-running-server detector.

These exercise the PURE decision core directly -- no server, no git, no OS calls --
so they run identically on the Linux CI runner and on a developer's Windows box. The
three behaviours the fix promises are asserted here by actually calling the functions:

1. a Python-side update with no live run  -> the running server is STALE and the
   post-update action is to SWAP it;
2. an update that touched nothing the server imports -> NO-OP (docs/ui-only pulls
   must not disturb a healthy server);
3. a Python-side update WHILE a fleet/review run is live -> REPORT ONLY, never a
   swap, so the running work is not dropped.

The thin CLI is also covered end-to-end (marker file present / stale / absent) via a
subprocess so that "what doctor runs" is what the tests check.
"""

from __future__ import annotations

import sys
from pathlib import Path

import stale_server_check as S


MODULE_PATH = Path(S.__file__).resolve()


# --------------------------------------------------------------- is_server_code_path

def test_relay_and_tools_and_main_are_server_code():
    assert S.is_server_code_path("relay/fleet_runner.py")
    assert S.is_server_code_path("tools/auth_stats.py")
    assert S.is_server_code_path("main.py")


def test_ui_docs_scripts_are_not_server_code():
    # These do not ride inside the running server process, so a change to them must
    # never provoke a server swap.
    assert not S.is_server_code_path("ui/FleetCockpit.cs")
    assert not S.is_server_code_path("docs/TROUBLESHOOTING.md")
    assert not S.is_server_code_path("scripts/doctor.ps1")
    # a file merely NAMED like the entrypoint but living elsewhere is not main.py
    assert not S.is_server_code_path("bench/main.py")


def test_path_normalization_backslash_and_dot_prefix():
    # git emits forward slashes, but a hand- or shell-built path must still classify.
    assert S.is_server_code_path("./relay/x.py")
    assert S.is_server_code_path("relay\\\\sub\\\\y.py")


# --------------------------------------------------------------- python_side_changed

def test_python_side_changed_true_when_any_server_file_touched():
    changed = ["ui/FleetCockpit.cs", "docs/README.md", "tools/tool_probe.py"]
    assert S.python_side_changed(changed) is True


def test_python_side_changed_false_for_ui_or_docs_only():
    assert S.python_side_changed(["ui/FleetCockpit.cs", "docs/README.md"]) is False
    assert S.python_side_changed([]) is False
    assert S.python_side_changed(["", "   "]) is False


# ------------------------------------------------------------------ classify_staleness

def test_no_server_means_nothing_to_be_stale():
    assert S.classify_staleness("abc", "def", server_running=False) == "no_server"


def test_running_at_same_sha_is_current():
    assert S.classify_staleness("abc123", "abc123", server_running=True) == "current"


def test_running_at_different_sha_is_stale():
    # THE CORE CASE: server started before the pull, checkout has moved on.
    assert S.classify_staleness("oldsha", "newsha", server_running=True) == "stale"


def test_missing_marker_or_head_is_unknown_not_a_pass():
    assert S.classify_staleness(None, "newsha", server_running=True) == "unknown"
    assert S.classify_staleness("", "newsha", server_running=True) == "unknown"
    assert S.classify_staleness("oldsha", "", server_running=True) == "unknown"


# ----------------------------------------------- classify_staleness watched_changed gate
# THE REPORT THIS ANSWERS (2026-09-17, owner msg #40): "main.py を触ると変わるなら
# エージェントで改修したら全黄になる=虚報" -- i.e. the dot going amber on an ordinary
# commit that moved HEAD but changed nothing the running server imports. aad4d22 added
# the watched_changed parameter so a different SHA is no longer staleness on its own; the
# three tests below PROVE the gate, which the SHA-only cases above never exercise.

def test_a_different_sha_that_touched_no_imported_code_is_not_stale():
    """THE VINDICATION OF THE REPORT. A docs-only, cockpit-only or tests-only commit moves
    HEAD -- oldsha != newsha -- yet changes nothing under WATCHED. Before the gate this was
    reported stale (amber) every time; now watched_changed=False keeps it current, so an
    agent's ordinary non-server edit does NOT turn the dot amber."""
    assert S.classify_staleness("oldsha", "newsha", server_running=True,
                                watched_changed=False) == "current"


def test_a_different_sha_that_did_touch_imported_code_is_still_stale():
    """The gate must not swing too far: a commit that DID change relay/tools/main.py while
    the server kept running is genuinely stale, and watched_changed=True preserves that --
    exactly the live case the whole check exists for, so main.py edits are not swept under."""
    assert S.classify_staleness("oldsha", "newsha", server_running=True,
                                watched_changed=True) == "stale"


def test_an_unknown_watched_changed_falls_back_to_the_conservative_sha_rule():
    """watched_changed=None means the caller could not tell what changed; the SHA rule then
    stands, because reporting stale when it might be is the safe side. Default (omitted) is
    None, so any caller that does not pass the flag keeps the pre-gate behaviour."""
    assert S.classify_staleness("oldsha", "newsha", server_running=True,
                                watched_changed=None) == "stale"
    assert S.classify_staleness("oldsha", "newsha", server_running=True) == "stale"


def test_the_gate_never_overrides_a_matching_sha_or_a_dead_server():
    """watched_changed only decides the different-SHA case. A matching SHA is current and a
    dead server is no_server whatever the flag says -- the gate cannot manufacture staleness
    where the SHA already settled it."""
    assert S.classify_staleness("same", "same", server_running=True,
                                watched_changed=True) == "current"
    assert S.classify_staleness("old", "new", server_running=False,
                                watched_changed=True) == "no_server"


# ----------------------------------------------- the import-scope the gate depends on

def test_an_ordinary_agent_edit_outside_watched_does_not_read_as_stale_end_to_end():
    """THE MEASUREMENT THE REPORT ASKED FOR, end to end rather than by hand. Build a real
    tree, start the clock, then make the edits an agent's ordinary non-server change makes --
    a doc, the cockpit C#, a test file, a data file -- all AFTER the server started. If
    deploy_freshness.newer_than finds nothing, watched_changed is False and the dot stays
    green. This is the same walk the supervisor's --server-action feeds into the gate."""
    import os
    import tempfile
    import time

    import tools.deploy_freshness as F

    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "tools"))
        os.makedirs(os.path.join(d, "relay"))
        os.makedirs(os.path.join(d, "docs"))
        os.makedirs(os.path.join(d, "ui"))
        for rel in ("main.py", os.path.join("tools", "a.py"),
                    os.path.join("relay", "b.py")):
            with open(os.path.join(d, rel), "w", encoding="utf-8") as fh:
                fh.write("x = 1\n")
        started = time.time()
        time.sleep(0.01)
        # the ordinary agent edits: NONE of them under a watched import path
        for rel in (os.path.join("docs", "NOTES.md"),
                    os.path.join("ui", "FleetCockpit.cs"),
                    os.path.join("tools", "test_a.py"),
                    os.path.join("tools", "data.json")):
            with open(os.path.join(d, rel), "w", encoding="utf-8") as fh:
                fh.write("touched\n")
        newer = F.newer_than(started, root=d)
        watched_changed = bool(newer)
        assert newer == [], "ordinary non-server edits must not be seen as imported: %r" % newer
        assert S.classify_staleness("oldsha", "newsha", server_running=True,
                                    watched_changed=watched_changed) == "current"


# -------------------------------------------------------------- decide_post_update_action

def test_scenario_1_python_update_no_run_is_swap_needed():
    changed = ["tools/auth_stats.py", "ui/FleetCockpit.cs"]
    py_changed = S.python_side_changed(changed)
    fleet = S.fleet_is_running([(False, False)])   # no live run
    assert py_changed is True
    assert S.decide_post_update_action(py_changed, fleet) == "swap-needed"


def test_scenario_2_ui_only_update_is_noop():
    changed = ["ui/FleetCockpit.cs", "docs/README.md"]
    py_changed = S.python_side_changed(changed)
    fleet = S.fleet_is_running([(False, False)])
    assert py_changed is False
    assert S.decide_post_update_action(py_changed, fleet) == "noop"


def test_scenario_3_python_update_during_live_run_is_report_only():
    changed = ["relay/fleet_runner.py"]
    py_changed = S.python_side_changed(changed)
    fleet = S.fleet_is_running([(True, True)])      # a run marker with a LIVE pid
    assert py_changed is True
    assert S.decide_post_update_action(py_changed, fleet) == "report-only"


def test_unchanged_python_is_noop_even_if_a_run_is_live():
    # Order matters: nothing to swap wins over a live run.
    assert S.decide_post_update_action(False, True) == "noop"


# ----------------------------------------------------------------------- fleet_is_running

def test_dead_pid_marker_does_not_count_as_running():
    # A crashed run leaves a marker whose pid is dead; that is not a live run.
    assert S.fleet_is_running([(True, False)]) is False


def test_any_live_marker_counts():
    assert S.fleet_is_running([(False, False), (True, True)]) is True
    assert S.fleet_is_running([]) is False


# ------------------------------------------------------------------------------- the CLI

def _run_cli(*args):
    from tools.childproc import run as _run_child
    proc = _run_child(
        [sys.executable, str(MODULE_PATH), *args], timeout=30,
    )
    return proc.returncode, proc.stdout.strip()


def test_cli_reports_stale_when_marker_differs_from_head(tmp_path):
    marker = tmp_path / "server_started_head.txt"
    marker.write_text("oldsha\n", encoding="utf-8")
    code, out = _run_cli(str(marker), "newsha", "1")
    assert code == 0
    assert out == "stale"


def test_cli_reports_current_when_marker_matches_head(tmp_path):
    marker = tmp_path / "server_started_head.txt"
    marker.write_text("samesha", encoding="utf-8")
    code, out = _run_cli(str(marker), "samesha", "1")
    assert code == 0
    assert out == "current"


def test_cli_reports_unknown_when_marker_absent(tmp_path):
    missing = tmp_path / "does_not_exist.txt"
    code, out = _run_cli(str(missing), "newsha", "1")
    assert code == 0
    assert out == "unknown"


def test_cli_reports_no_server_when_flag_is_zero(tmp_path):
    marker = tmp_path / "server_started_head.txt"
    marker.write_text("oldsha", encoding="utf-8")
    code, out = _run_cli(str(marker), "newsha", "0")
    assert code == 0
    assert out == "no_server"


def test_cli_usage_error_without_enough_args():
    code, out = _run_cli()
    assert code == 2
    assert out == "error:usage"


def test_cli_pyside_yes_when_server_code_in_args():
    code, out = _run_cli("--pyside", "ui/x.cs", "tools/tool_probe.py")
    assert code == 0
    assert out == "yes"


def test_cli_pyside_no_for_ui_only_args():
    code, out = _run_cli("--pyside", "ui/x.cs", "docs/readme.md")
    assert code == 0
    assert out == "no"


def test_cli_pyside_reads_stdin_when_no_paths_follow_flag():
    from tools.childproc import run as _run_child
    proc = _run_child(
        [sys.executable, str(MODULE_PATH), "--pyside"],
        input=b"ui/x.cs\nrelay/fleet_runner.py\n", timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "yes"


def test_a_folder_without_git_passes_the_staleness_check_instead_of_staying_indeterminate():
    """A ZIP unpacked on a new PC has no .git, HEAD never resolves, and the check used to answer
    "could not be determined" on every run -- so quickstart.bat ended in SETUP NOT CONFIRMED for
    good, for exactly the people installing from the release ZIP. Such a folder is updated by
    unpacking a new ZIP, so the question does not apply. The guard must come BEFORE the git call,
    and a .git that exists while git fails must stay indeterminate."""
    text = (Path(__file__).resolve().parent / "doctor.ps1").read_text(encoding="utf-8-sig")
    block = text[text.index('Check-TriState "server_not_stale"'):]
    block = block[:block.index("auth_bearer")]
    guard = block.index('if (-not (Test-Path (Join-Path $repo ".git"))) { return $true }')
    git_call = block.index("rev-parse HEAD")
    assert guard < git_call, "the no-.git pass must precede the git call"
    assert "if ($LASTEXITCODE -ne 0 -or -not $head) { return $null }" in block, \
        "a real git failure inside a checkout must stay indeterminate"


# ------------------------------------------------------------------ decide_stale_drain
# The drain gate the supervisor consults before cycling a stale server. It is pure: the
# caller reads /health (resp_inflight, resp_last_start_age_s), accumulates the seconds it
# has already waited, and acts on the one word this returns. These assert the three
# behaviours the gate promises -- hold while a request is open, hold just after one
# started, and never hold past the cap -- by calling the function directly, so they run
# identically on the Linux CI runner and a developer's Windows box.

def test_drain_waits_while_a_request_is_in_flight():
    # A client is mid-response: cycling now would cut it. Hold.
    assert S.decide_stale_drain(1, 0.3, 0) == "wait"
    assert S.decide_stale_drain(3, 42.0, 0) == "wait"


def test_drain_waits_right_after_a_request_started_even_with_zero_inflight():
    # resp_inflight can read zero for the instant between the relay accepting a request and
    # the telemetry wrapper opening it. A fresh start (age < settle) is still "busy".
    assert S.decide_stale_drain(0, 0.0, 0) == "wait"
    assert S.decide_stale_drain(0, 4.9, 0) == "wait"


def test_drain_cycles_when_nothing_is_in_flight_and_nothing_just_arrived():
    # No open request and the last one started well past the settle window: safe to cycle.
    assert S.decide_stale_drain(0, 5.0, 0) == "cycle"
    assert S.decide_stale_drain(0, 900.0, 0) == "cycle"


def test_drain_cycles_when_no_request_has_started_since_startup():
    # age is None when the ring has seen no request at all. Nothing to protect -> cycle.
    assert S.decide_stale_drain(0, None, 0) == "cycle"


def test_drain_cap_forces_a_cycle_even_with_a_request_still_open():
    # THE BACKSTOP. A wedged client that never completes must not keep stale code live
    # forever: once waited reaches the cap, cycle regardless of inflight or age.
    assert S.decide_stale_drain(5, 1.0, 180) == "cycle"
    assert S.decide_stale_drain(5, 1.0, 181) == "cycle"
    # ONE TICK SHORT OF THE CAP STILL WAITS, so the cap boundary is exact, not approximate.
    assert S.decide_stale_drain(5, 1.0, 179) == "wait"


def test_drain_cap_is_checked_before_everything_else():
    # Even with a request open and freshly started, the cap wins -- it is the only thing that
    # guarantees the fix eventually goes live.
    assert S.decide_stale_drain(2, 0.0, 180) == "cycle"


def test_drain_tolerates_a_missing_or_broken_age():
    # A damaged age can only ever make the gate MORE willing to cycle, never wedge it.
    assert S.decide_stale_drain(0, None, 0) == "cycle"
    assert S.decide_stale_drain(0, -3.0, 0) == "cycle"      # clock went backwards
    assert S.decide_stale_drain(0, "not-a-number", 0) == "cycle"
    # but a real open request still holds, whatever the age reads.
    assert S.decide_stale_drain(1, None, 0) == "wait"


def test_drain_tolerates_a_broken_inflight_or_waited():
    # Non-numeric inflight reads as zero (nothing proven in flight); non-numeric waited reads
    # as zero seconds waited. Neither can wedge the gate.
    assert S.decide_stale_drain("x", 900.0, 0) == "cycle"
    assert S.decide_stale_drain(1, 900.0, "x") == "wait"
