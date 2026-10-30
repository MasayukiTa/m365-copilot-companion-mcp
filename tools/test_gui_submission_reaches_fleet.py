# -*- coding: utf-8 -*-
"""A goal submitted from the Fleet Cockpit must reach the fleet's intake even when no
supervisor is running.

WHY THIS EXISTS. The cockpit's "追加" button does not call fleet_submit. It writes an
add_goal command into <state_dir>/commands.d/ (ui/FleetCockpit.Submit.cs:270-292,
relay/task_router.write_command). The ONLY consumer of commands.d is the live runner's
_drain_commands (relay/fleet_runner.py:4803), which runs ONLY while a supervisor run is in
flight. So when the owner typed an instruction into the cockpit while no run was draining
(2026-10-30 14:36), the add_goal sat in commands.d with no jid issued and never reached
tasks/pending -- the health strip showed it queued with id="" and unconfirmed=true for 6+
minutes. A goal submitted through the CLI (fleet_submit) was accepted in seconds because it
writes tasks/pending directly and never touches commands.d.

THE CONTRACT THIS PINS. The intake must promote an add_goal left in commands.d into
tasks/pending (issuing a jid) regardless of whether a supervisor is draining, so EVERY job
is admitted with no force step. Promotion must be atomic (claim-then-commit, the same seam
the runner uses) so a live runner and the intake sweep cannot both admit the same command.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay import task_router as TR  # noqa: E402
from relay import fleet_runner as FR  # noqa: E402
from tools import fleet_intake as FI  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """A state dir and task tree of their own. These tests write commands and jobs; writing
    them into the real .fleet would put work in front of whatever drains it."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(TR, "TASKS", str(state / "tasks"))
    TR.ensure_dirs()
    # The intake sweep must decide to run when no supervisor holds the mutex. Force the
    # "nobody is draining" answer so the test does not depend on the host's live fleet.
    monkeypatch.setattr(FI, "_consumer_is_draining", lambda: False, raising=False)
    return state


def _pending_jobs():
    d = os.path.join(TR.TASKS, "pending")
    out = []
    for name in sorted(os.listdir(d)):
        if name.endswith(".json"):
            with open(os.path.join(d, name), encoding="utf-8") as fh:
                out.append(json.load(fh))
    return out


def _commands_left(state):
    d = os.path.join(str(state), "commands.d")
    try:
        return [n for n in os.listdir(d) if n.endswith(".json")]
    except OSError:
        return []


def test_a_cockpit_add_goal_reaches_pending_without_a_supervisor(isolated_state):
    """The core defect: an add_goal in commands.d, no runner draining, must still land in
    tasks/pending with a real jid."""
    state = isolated_state
    TR.write_command(str(state), {"add_goal": {"text": "サイズ変更でよろしく。30%くらいに。"}})
    admitted = FI.admit_commands_to_pending(str(state))
    assert admitted == 1
    jobs = _pending_jobs()
    assert len(jobs) == 1
    assert jobs[0]["payload"]["goal"].startswith("サイズ変更")
    assert jobs[0]["id"] and len(jobs[0]["id"]) >= 8
    # The command was consumed, not left to be admitted a second time.
    assert _commands_left(state) == []


def test_admission_is_idempotent_on_the_same_command(isolated_state):
    """A second sweep over an already-consumed command must not create a duplicate job."""
    state = isolated_state
    TR.write_command(str(state), {"add_goal": {"text": "one instruction"}})
    assert FI.admit_commands_to_pending(str(state)) == 1
    assert FI.admit_commands_to_pending(str(state)) == 0
    assert len(_pending_jobs()) == 1


def test_non_add_goal_commands_are_left_for_the_runner(isolated_state):
    """steer/stop and other live-control commands are the runner's business. The intake
    sweep must not consume them -- doing so would swallow an order meant for a live run."""
    state = isolated_state
    TR.write_command(str(state), {"steer": {"worker": "w1", "text": "focus here"}})
    admitted = FI.admit_commands_to_pending(str(state))
    assert admitted == 0
    assert len(_pending_jobs()) == 0
    assert len(_commands_left(state)) == 1


def test_provenance_marks_the_goal_as_ui_submitted(isolated_state):
    """A promoted goal must carry an origin so the queue can tell a cockpit submission from a
    cli/mcp one, the same distinction fleet_submit records at its own door."""
    state = isolated_state
    TR.write_command(str(state), {"add_goal": {"text": "label me"}})
    FI.admit_commands_to_pending(str(state))
    job = _pending_jobs()[0]
    origin = job.get("origin") or {}
    assert str(origin.get("via", "")).startswith("ui")


def test_the_sweep_stands_down_while_a_supervisor_is_draining(isolated_state, monkeypatch):
    """When a supervisor IS draining commands.d, the intake sweep must not also admit: the
    runner owns the channel and a double-admit would queue the goal twice. The command is
    left in place for the live drain."""
    state = isolated_state
    monkeypatch.setattr(FI, "_consumer_is_draining", lambda: True, raising=False)
    TR.write_command(str(state), {"add_goal": {"text": "leave me for the runner"}})
    admitted = FI.admit_commands_to_pending(str(state))
    assert admitted == 0
    assert len(_pending_jobs()) == 0
    assert len(_commands_left(state)) == 1


def test_reading_the_queue_admits_a_waiting_cockpit_goal(isolated_state):
    """THE WIRING, not just the function. The cockpit writes an add_goal into commands.d and then
    re-reads the queue; fleet_queue must admit any waiting command first, so the goal the owner
    just typed appears as a counted pending job rather than sitting invisibly in commands.d. This
    is the seam that was missing: admit_commands_to_pending existed nowhere on the queue-read path,
    so a goal with no supervisor draining was never promoted. state_dir is derived from TR.TASKS
    (its parent), the same way the live door locates .fleet, so this also pins that derivation."""
    state = isolated_state
    TR.write_command(str(state), {"add_goal": {"text": "show me in the queue"}})
    out = FI.fleet_queue()
    # The goal was promoted as part of reading the queue...
    assert len(_pending_jobs()) == 1
    assert _commands_left(state) == []
    # ...and the queue count the owner sees reflects it.
    pending_line = [ln for ln in out.splitlines() if ln.startswith("pending")]
    assert pending_line and pending_line[0].split()[-1] == "1"
