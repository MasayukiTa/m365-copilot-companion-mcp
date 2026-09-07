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

import subprocess
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
    proc = subprocess.run(
        [sys.executable, str(MODULE_PATH), *args],
        capture_output=True, text=True, timeout=30,
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
    proc = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--pyside"],
        input="ui/x.cs\nrelay/fleet_runner.py\n",
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "yes"
