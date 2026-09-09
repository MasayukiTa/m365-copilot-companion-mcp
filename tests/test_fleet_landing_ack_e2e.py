"""The landing-ack JOIN between the goal SENDER and the fleet RECEIVER.

The fix these tests guard has two halves in two separately tested files:

  * relay/task_router.py   -- add_goal_to_live_fleet writes the command WITH an `ack`
    path, and fleet_landing_confirmed reads that ack back.
  * relay/fleet_runner.py  -- read_commands consumes the command and drops the ack
    receipt the instant before it deletes the command.

The fleet_runner docstring on goals_from_command names the exact hazard this file
exists to close: the writer of the channel and its reader were each covered by
their own tests and the JOIN between them by none -- the same shape as two shipped
defects (`gate_verdict` vs `verdict`, `keep` vs `kept`) where both sides passed
their own tests. So these tests drive the REAL sender and the REAL receiver against
one shared state dir and assert they meet on the same key and the same path.

FLEET_STATE_DIR is redirected by conftest, but the two functions under test both
take state_dir explicitly, so each test passes its own tmp dir and touches nothing
else.
"""
import json
import os

import pytest

from relay import task_router as tr
from relay import fleet_runner as fr


@pytest.fixture
def state(tmp_path):
    """One shared state dir the sender and receiver both use."""
    return str(tmp_path / "fleet_state")


def _commands(state_dir):
    d = os.path.join(state_dir, fr.COMMANDS_DIR)
    return sorted(n for n in os.listdir(d) if n.endswith(".json")) if os.path.isdir(d) else []


def test_sender_writes_ack_path_the_receiver_reads(state):
    """The one key the JOIN turns on: sender's `ack` == the path read_commands writes.

    This is the `gate_verdict`/`verdict` class of bug made impossible: if the sender
    ever renamed the key or the receiver read a different one, the command below would
    carry an ack the runner never honours, and the confirmation downstream would be a
    permanent false negative. Asserting the exact string keeps both halves in step.
    """
    jid = "job-abc"
    tr.add_goal_to_live_fleet("do the thing", state_dir=state, jid=jid)

    names = _commands(state)
    assert len(names) == 1, "the goal must land as exactly one command file"
    with open(os.path.join(state, fr.COMMANDS_DIR, names[0]), encoding="utf-8-sig") as fh:
        cmd = json.load(fh)

    assert cmd.get("ack") == tr._ack_path(jid, state)
    assert fr.goals_from_command(cmd) == [{"text": "do the thing", "priority": False}]


def test_landing_confirmed_only_after_the_runner_consumes_it(state):
    """dispatched is not delivered: confirmation flips only when read_commands runs."""
    jid = "job-landed"
    tr.add_goal_to_live_fleet("land me", state_dir=state, jid=jid)

    assert tr.fleet_landing_confirmed(jid, state) is False

    cmds = fr.read_commands(state)
    assert any(fr.goals_from_command(c) for c in cmds), "the goal must come back out"

    assert tr.fleet_landing_confirmed(jid, state) is True
    assert _commands(state) == []


def test_receipt_records_which_command_file_was_read(state):
    """The receipt is auditable: it names the command file it confirms, not just True."""
    jid = "job-audit"
    tr.add_goal_to_live_fleet("audit me", state_dir=state, jid=jid)
    written = _commands(state)
    assert len(written) == 1

    fr.read_commands(state)

    with open(tr._ack_path(jid, state), encoding="utf-8") as fh:
        receipt = json.load(fh)
    assert receipt.get("read") is True
    assert receipt.get("file") == written[0]
    assert isinstance(receipt.get("ts"), (int, float))


def test_goal_without_jid_leaves_no_ack_and_still_delivers(state):
    """A caller that does not ask for confirmation must not get a broken command."""
    tr.add_goal_to_live_fleet("no receipt please", state_dir=state)
    names = _commands(state)
    assert len(names) == 1
    with open(os.path.join(state, fr.COMMANDS_DIR, names[0]), encoding="utf-8-sig") as fh:
        cmd = json.load(fh)
    assert "ack" not in cmd

    cmds = fr.read_commands(state)
    assert any(fr.goals_from_command(c) for c in cmds)
    assert not os.path.isdir(os.path.join(state, tr.ACKS_DIR))


def test_two_goals_each_get_their_own_receipt(state):
    """Distinct jids do not clobber: each landing is confirmed independently."""
    tr.add_goal_to_live_fleet("first", state_dir=state, jid="j1")
    tr.add_goal_to_live_fleet("second", state_dir=state, jid="j2")
    assert len(_commands(state)) == 2

    fr.read_commands(state)

    assert tr.fleet_landing_confirmed("j1", state) is True
    assert tr.fleet_landing_confirmed("j2", state) is True
    assert tr.fleet_landing_confirmed("j3-never-sent", state) is False
