# -*- coding: utf-8 -*-
"""A GUI fresh-Start must not leave a "投入済" card with no run behind it.

## What happened (2026-10-26 09:24-09:30, read off .fleet logs)

Four tasks were submitted from Fleet Cockpit and stuck at "投入済・未着手" for 6 then 11
minutes; nothing reached the fleet until a manual re-submit. The chain:

1. The PRIOR coordinator printed "fleet complete" but then spent ~1m48s in post-run cleanup
   (relay/fleet_runner.py main() tail) while STILL HOLDING the state-dir start lock -- the
   heavy part being report_duplicate_completions(), which reads and reconciles every
   transcript on disk, ran BEFORE the lock was released.
2. The GUI's SpawnFleet launched a new coordinator with --wait-for-state-dir-seconds 60 and
   raised the "投入済" card (NoteSubmitted) IMMEDIATELY, before the new run was accepted.
3. The new coordinator waited 60s < 1m48s, printed "TIMED OUT WAITING TO START", exited 3.
   Its stdout was not redirected anywhere, so the reason was invisible.
4. WatchFreshFleetLaunch showed the exit code but never withdrew the card, and did not retry.

## What this checks (source-string checks, the house style for the WPF paths pytest
## cannot import)

No C# is compiled here: ui/FleetCockpit.cs is WPF. These assert the SHIPPED source of the
two files the fix touches encodes each decision, the same way e.g.
ui/test_a_ledger_the_ui_writes_has_no_byte_order_mark.py and several relay/test_*.py assert
against source text.

1. fleet_runner.py releases the start lock (_clear_active_marker + _release_run_lock) BEFORE
   it calls report_duplicate_completions, not after -- so a waiting fresh-Start is not blocked
   by end-of-run reporting.
2. SpawnFleet redirects the coordinator's stdout AND stderr and writes them to a
   .fleet/gui_start_<...>.log file.
3. On the waitForClosingRun path the "投入済" card is raised from WatchFreshFleetLaunch at
   acceptance, NOT at spawn time.
4. A TIMED-OUT launch (exit code 3) is retried up to a bounded budget before standing down.
"""
from __future__ import annotations

import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUBMIT = os.path.join(REPO, "ui", "FleetCockpit.Submit.cs")
RUNNER = os.path.join(REPO, "relay", "fleet_runner.py")


def _read(path):
    with open(path, encoding="utf-8-sig") as f:
        return f.read()


def test_runner_releases_start_lock_before_duplicate_report():
    """The lock release must precede report_duplicate_completions in main()'s tail."""
    src = _read(RUNNER)
    rel = src.find("_release_run_lock(_ACTIVE_RUN_LOCK)")
    rep = src.find("report_duplicate_completions(args.state_dir)")
    assert rel != -1, "the start-lock release call is gone from fleet_runner.py"
    assert rep != -1, "report_duplicate_completions call is gone from fleet_runner.py"
    assert rel < rep, (
        "report_duplicate_completions (reads every transcript) must run AFTER the start lock "
        "is released, or a GUI fresh-Start waiting on that lock times out -- this is the "
        "2026-10-26 regression"
    )
    # And the marker clear belongs with the release, still ahead of the report.
    clr = src.find("_clear_active_marker(args.state_dir, owner_pid=os.getpid())")
    assert clr != -1 and clr < rep, "ACTIVE marker must be cleared before the duplicate report too"


def test_spawn_redirects_coordinator_output_to_a_file():
    src = _read(SUBMIT)
    assert "RedirectStandardOutput = true" in src, "coordinator stdout is not redirected"
    assert "RedirectStandardError = true" in src, "coordinator stderr is not redirected"
    assert "gui_start_" in src, "the captured output is not written to a .fleet/gui_start_<...>.log file"
    # The streams are drained asynchronously (a blocking ReadToEnd on the UI thread could deadlock).
    assert "BeginOutputReadLine" in src and "BeginErrorReadLine" in src, (
        "the redirected streams must be drained asynchronously"
    )


def test_card_is_raised_at_acceptance_not_at_spawn():
    """On the waiting path, NoteSubmitted fires inside WatchFreshFleetLaunch's accepted branch,
    and the spawn site no longer raises it unconditionally before the run is accepted."""
    src = _read(SUBMIT)
    watch = src.find("void WatchFreshFleetLaunch")
    assert watch != -1, "WatchFreshFleetLaunch is gone"
    accepted = src.find("if (accepted)", watch)
    assert accepted != -1, "the accepted branch is gone from WatchFreshFleetLaunch"
    note_in_accepted = src.find("NoteSubmitted(", accepted)
    # NoteSubmitted must appear shortly after the accepted branch opens (within the branch body,
    # ahead of the next method). The slack accommodates the explanatory comment above the call.
    next_method = src.find("\n    void ", accepted)
    assert note_in_accepted != -1 and (next_method == -1 or note_in_accepted < next_method), (
        "the 投入済 card must be raised when the fresh run is confirmed accepted"
    )
    assert note_in_accepted - accepted < 700, (
        "NoteSubmitted must be inside the accepted branch, not further down the method"
    )
    # The waitForClosingRun spawn branch must NOT raise the card before watching.
    spawn = src.find("bool SpawnFleet(List<string> goals")
    wait_branch = src.find("if (waitForClosingRun)", spawn)
    assert wait_branch != -1
    watch_call = src.find("WatchFreshFleetLaunch(proc, submitBaseline", wait_branch)
    assert watch_call != -1, "the waiting spawn branch must hand off to WatchFreshFleetLaunch"
    between = src[wait_branch:watch_call]
    assert "NoteSubmitted(" not in between, (
        "the waiting spawn branch must NOT raise the 投入済 card before the run is accepted"
    )


def test_timed_out_launch_is_retried_within_a_bounded_budget():
    src = _read(SUBMIT)
    assert "FreshLaunchMaxAttempts" in src, "no retry budget constant for the fresh-Start path"
    m = re.search(r"const int FreshLaunchMaxAttempts\s*=\s*(\d+)", src)
    assert m, "FreshLaunchMaxAttempts must be a concrete small constant"
    attempts = int(m.group(1))
    assert 2 <= attempts <= 5, (
        "the retry budget should be small and bounded (chosen: 2); got %d" % attempts
    )
    # Exit code 3 is the TIMED OUT signal and must drive the retry decision.
    assert "code == 3" in src, "exit code 3 (TIMED OUT WAITING TO START) must gate the retry"
    assert "SpawnFleetRetry" in src, "there must be a retry spawn arm for the timed-out launch"
