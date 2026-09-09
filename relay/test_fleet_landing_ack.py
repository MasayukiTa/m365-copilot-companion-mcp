# -*- coding: utf-8 -*-
"""A dispatched fleet goal is only DELIVERED once the fleet has read it.

WHAT WAS WRONG. fleet_handoff filed a goal "dispatched" the instant fleet_is_live() returned
True. But fleet_is_live trusts a status.json up to FLEET_LIVE_MAX_AGE_S (30s) old, so a run
that had already died still read as live for up to 30 seconds. A goal queued in that window
went into commands.d/ that no running fleet would ever read -- consumed-on-read by no one --
while its done/ record said "dispatched". Nothing on the receiving side recorded that a
command had been taken, so the sender's claim of delivery could never be checked against the
truth. Reproduced by probe on 2026-09-07: proc dead, status fresh, fleet_is_live True,
command written, no ack directory ever created.

WHAT THE FIX ADDS. The command now carries an `ack` path; relay/fleet_runner.read_commands
drops a receipt there the instant before it deletes the command it just READ. So a goal a
live fleet actually picked up leaves a receipt, and a goal lost to the stale window leaves
none -- and fleet_landing_confirmed(jid) reports exactly that difference. These assertions go
through the fleet's OWN reader (read_commands), not a copy of it, so a change to either side
shows up here.
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay import task_router as TR  # noqa: E402
from relay import fleet_runner as FR  # noqa: E402


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(tmp_path / "fleet"))
    os.makedirs(str(tmp_path / "fleet"), exist_ok=True)
    TR.ensure_dirs()
    return tmp_path / "fleet"


def _live(state_dir, running=True, age_s=0.0):
    p = os.path.join(str(state_dir), "status.json")
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"running": running, "updated": time.time()}, fh)
    if age_s:
        old = time.time() - age_s
        os.utime(p, (old, old))


def test_a_goal_a_live_fleet_reads_leaves_a_confirmation(state):
    _live(state)
    status, result = TR.fleet_handoff("確認できるゴール", "jok", str(state))
    assert status == "dispatched" and result["delivered"] == "add_goal"
    assert TR.fleet_landing_confirmed("jok", str(state)) is False
    got = FR.read_commands(str(state))
    assert [g["text"] for c in got for g in FR.goals_from_command(c)] == ["確認できるゴール"]
    assert TR.fleet_landing_confirmed("jok", str(state)) is True, (
        "the fleet read the command but left no receipt -- delivery is unverifiable again")


def test_a_goal_lost_to_the_stale_window_is_never_confirmed(state):
    _live(state, running=True, age_s=TR.FLEET_LIVE_MAX_AGE_S - 1)
    assert TR.fleet_is_live(str(state)) is True
    status, _ = TR.fleet_handoff("消える窓のゴール", "jlost", str(state))
    assert status == "dispatched", "handoff still queues it; the destination is right"
    assert TR.fleet_landing_confirmed("jlost", str(state)) is False, (
        "a goal no fleet read must not look delivered")


def test_a_stale_receipt_from_a_previous_run_does_not_count(state):
    _live(state)
    ackp = TR._ack_path("jreuse", str(state))
    os.makedirs(os.path.dirname(ackp), exist_ok=True)
    with open(ackp, "w", encoding="utf-8") as fh:
        fh.write("{}")
    TR.fleet_handoff("再送のゴール", "jreuse", str(state))
    assert TR.fleet_landing_confirmed("jreuse", str(state)) is False, (
        "a stale receipt made an unread goal look delivered")
    FR.read_commands(str(state))
    assert TR.fleet_landing_confirmed("jreuse", str(state)) is True


def test_the_ack_key_does_not_disturb_what_the_reader_extracts(state):
    """The `ack` key sits on the COMMAND, not the item -- it must never leak into what
    goals_from_command extracts. `jid`, added 2026-09-09 (codex-plan item 1), is different:
    it is deliberately placed ON the item by add_goal_to_live_fleet so the goal's admission
    id survives into the worker that runs it, so it belongs in the expected shape here."""
    _live(state)
    TR.fleet_handoff("内容は不変", "jrt", str(state))
    goals = [g for c in FR.read_commands(str(state)) for g in FR.goals_from_command(c)]
    assert goals == [{"text": "内容は不変", "priority": False, "jid": "jrt"}], (
        "the ack path leaked into the goal the reader extracted: %r" % (goals,))
