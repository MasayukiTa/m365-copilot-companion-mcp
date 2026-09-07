# -*- coding: utf-8 -*-
"""Delivery is proven by the RECEIVER's ack, not the sender's own record.

The fleet handoff used to write for_fleet/<id>.txt, call the goal "dispatched" and
stop -- an attestation written whether or not any fleet ever read the command. A run
that ended inside the up-to-30s window fleet_is_live cannot see still looked live, so a
goal was written into a dead command channel and filed as delivered.

These tests drive BOTH real halves end to end -- the sender in relay/task_router and
the receiver's own consumer in relay/fleet_runner.read_commands -- with no live
process. The point measured on the machine, before the fix, was the exact split this
asserts against: two relay.fleet_runner processes were alive for ~57 minutes while
fleet_is_live() returned False on five consecutive samples because no status.json was
fresh. A liveness check that ignores the running process is the reason the sender's
"dispatched" could not be trusted; here the ack stamp -- placed by the reader as it
consumes the command -- is what the sender waits on instead.
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
    """Point both the sender and the reader at the same throwaway state dir."""
    fleet = tmp_path / "fleet"
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(fleet))
    os.makedirs(str(fleet), exist_ok=True)
    TR.ensure_dirs()
    # A near-zero ack wait keeps the no-ack cases from sleeping the default 8s.
    monkeypatch.setattr(TR, "FLEET_ACK_WAIT_S", 0.2)
    return str(fleet)


def _mark_live(state_dir, running=True, age_s=0.0):
    """Write the status.json fleet_is_live reads, optionally aged past the window."""
    p = os.path.join(state_dir, "status.json")
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"running": running, "updated": time.time()}, fh)
    if age_s:
        old = time.time() - age_s
        os.utime(p, (old, old))
    return p


# --- the receiver half: read_commands stamps the ack as it consumes -------------------

def test_read_commands_stamps_ack_for_consumed_goal(state):
    """The reader writes acked/<ack>.json exactly when it takes the command in."""
    ack = TR.add_goal_to_live_fleet("do the thing", state)
    assert ack, "add_goal_to_live_fleet must return an ack nonce to wait on"
    # Not stamped until something actually reads the command channel.
    assert TR.ack_seen(ack, state) is False
    consumed = FR.read_commands(state)
    assert any(
        item.get("ack") == ack
        for cmd in consumed
        for item in (cmd.get("add_goal") or [])
    ), "the reader must return the very command that carried this ack"
    assert TR.ack_seen(ack, state) is True


def test_ack_is_stamped_only_after_the_read_not_on_write(state):
    """Writing the command is not delivery; consuming it is."""
    ack = TR.add_goal_to_live_fleet("queued but unread", state)
    assert TR.ack_seen(ack, state) is False
    # A second, independent goal does not stamp the first.
    other = TR.add_goal_to_live_fleet("a different goal", state)
    assert TR.ack_seen(ack, state) is False
    assert other != ack
    FR.read_commands(state)
    assert TR.ack_seen(ack, state) is True
    assert TR.ack_seen(other, state) is True


def test_stamp_acks_skips_items_without_a_nonce(state):
    """An older sender or the C# cockpit omits `ack`; those are simply not stamped."""
    # Hand-write a command with no ack, exactly as a pre-fix / non-Python writer would.
    TR.write_command(state, {"add_goal": [{"text": "legacy goal", "priority": False}]})
    before = os.listdir(os.path.join(state, FR.ACKED_DIR)) if os.path.isdir(
        os.path.join(state, FR.ACKED_DIR)) else []
    consumed = FR.read_commands(state)
    assert consumed, "the command must still be delivered even without an ack"
    after = os.listdir(os.path.join(state, FR.ACKED_DIR)) if os.path.isdir(
        os.path.join(state, FR.ACKED_DIR)) else []
    assert before == after, "a goal with no ack nonce must not produce a stamp"


# --- the sender half: fleet_handoff waits for the receiver's proof ---------------------

def test_handoff_reports_dispatched_only_after_the_ack_lands(state):
    """With a live status.json AND the reader consuming, the goal is truly dispatched."""
    _mark_live(state, running=True)
    assert TR.fleet_is_live(state) is True
    status, result = None, None

    # Consume the command channel concurrently so the ack lands within the wait window.
    import threading

    def drain():
        # Give the sender a moment to park + write the command, then consume it.
        for _ in range(50):
            got = FR.read_commands(state)
            if got:
                return
            time.sleep(0.01)

    t = threading.Thread(target=drain)
    t.start()
    status, result = TR.fleet_handoff("real goal", "job-ackd", state)
    t.join()

    assert status == "dispatched", result
    assert result.get("delivered") == "add_goal"
    assert result.get("ack")
    # Parked file is removed once delivery is confirmed.
    assert not os.path.isfile(os.path.join(state, "for_fleet", "job-ackd.txt"))


def test_handoff_awaits_when_live_but_no_ack_arrives(state):
    """status.json says running, but nothing consumes: do NOT claim delivery.

    This is the run-ended-inside-the-window case the machine measurement reproduced:
    the sender saw 'live', wrote the command, and no reader was there to stamp it.
    """
    _mark_live(state, running=True)
    assert TR.fleet_is_live(state) is True
    status, result = TR.fleet_handoff("orphaned goal", "job-orphan", state)
    assert status == "awaiting_fleet", result
    assert result.get("ack"), "an ack was issued even though it was never stamped"
    # The goal is LEFT PARKED on disk for retry -- not silently filed as delivered.
    # Parked under the job queue's for_fleet/, which _park_in_for_fleet places via TASKS.
    parked = os.path.join(TR.TASKS, "for_fleet", "job-orphan.txt")
    assert os.path.isfile(parked)
    with open(parked, encoding="utf-8") as fh:
        assert fh.read() == "orphaned goal"


def test_handoff_awaits_when_not_live_and_parks_the_goal(state):
    """No fresh status.json at all: waiting, with the goal visible on disk."""
    # A stale status.json claiming running=True must not count as live.
    _mark_live(state, running=True, age_s=TR.FLEET_LIVE_MAX_AGE_S + 5)
    assert TR.fleet_is_live(state) is False
    status, result = TR.fleet_handoff("waiting goal", "job-wait", state)
    assert status == "awaiting_fleet", result
    parked = os.path.join(TR.TASKS, "for_fleet", "job-wait.txt")
    assert os.path.isfile(parked)
