# -*- coding: utf-8 -*-
"""The FLEET destination filed every job as delivered and delivered none of them.

run_job's fleet branch wrote for_fleet/<id>.txt, set status "dispatched", and stopped. No file
in relay/, bridge/, tools/, ui/ or scripts/ read that directory -- the handoff had no other
end. So the router's own docstring promise, "the fleet runner completes them and writes
done/", was never true, and the queue's .fleet/tasks directories are empty of history because
nothing ever ran.

The delivery now happens the way relay/code_task.py already does it, and for the reason
recorded there: two fleets share the one dedicated Edge and clobber each other's status.json,
and the second run's work appeared as a phantom worker behind the first. So a goal joins the
run that is in flight rather than starting a competing one.
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay import task_router as TR  # noqa: E402


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(tmp_path / "fleet"))
    os.makedirs(str(tmp_path / "fleet"), exist_ok=True)
    TR.ensure_dirs()
    return tmp_path / "fleet"


def _status(state_dir, running, age_s=0.0):
    p = os.path.join(str(state_dir), "status.json")
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"running": running, "updated": time.time()}, fh)
    if age_s:
        old = time.time() - age_s
        os.utime(p, (old, old))
    return p


def _commands(state_dir):
    p = os.path.join(str(state_dir), "commands.json")
    if not os.path.isfile(p):
        return {}
    with open(p, encoding="utf-8-sig") as fh:
        return json.load(fh)


# -- is a fleet actually running ---------------------------------------------------------------

def test_a_live_run_is_recognised(state):
    _status(state, True)
    assert TR.fleet_is_live(str(state)) is True


def test_a_finished_run_is_not_live(state):
    _status(state, False)
    assert TR.fleet_is_live(str(state)) is False


def test_a_stale_snapshot_is_not_live_however_it_reads(state):
    """A fleet that died leaves its last snapshot behind. Trusting running=True in a file
    nobody has touched for an hour queues every later goal into a run that ended."""
    _status(state, True, age_s=TR.FLEET_LIVE_MAX_AGE_S + 10)
    assert TR.fleet_is_live(str(state)) is False


def test_no_snapshot_at_all_is_not_live(state):
    assert TR.fleet_is_live(str(state)) is False


# -- delivery ------------------------------------------------------------------------------------

def test_a_goal_joins_the_run_that_is_in_flight(state):
    _status(state, True)
    status, result = TR.fleet_handoff("先月のメールを一覧して", "j1", str(state))
    assert status == "dispatched" and result["delivered"] == "add_goal"
    adds = _commands(state)["add_goal"]
    assert len(adds) == 1 and adds[0]["text"] == "先月のメールを一覧して"
    assert adds[0]["priority"] is False, "an inbound goal must not jump the current work"


def test_a_second_goal_is_appended_not_replacing_the_first(state):
    _status(state, True)
    TR.fleet_handoff("first", "j1", str(state))
    TR.fleet_handoff("second", "j2", str(state))
    assert [a["text"] for a in _commands(state)["add_goal"]] == ["first", "second"]


def test_other_commands_in_the_file_survive(state):
    """commands.json is shared with the cockpit's stop/pause. Rewriting it wholesale would
    drop a pause a human had just set."""
    _status(state, True)
    with open(os.path.join(str(state), "commands.json"), "w", encoding="utf-8") as fh:
        json.dump({"pause": True}, fh)
    TR.fleet_handoff("a goal", "j1", str(state))
    cur = _commands(state)
    assert cur["pause"] is True and len(cur["add_goal"]) == 1


def test_the_file_is_written_without_a_bom(state):
    """The fleet reads utf-8-sig, so a BOM parses -- but code_task.py writes none, and two
    writers of one file should write it the same way."""
    _status(state, True)
    TR.fleet_handoff("a goal", "j1", str(state))
    with open(os.path.join(str(state), "commands.json"), "rb") as fh:
        assert not fh.read(3).startswith(b"\xef\xbb\xbf")


# -- what it does NOT do ---------------------------------------------------------------------------

def test_with_no_run_in_flight_the_goal_waits_and_says_so(state):
    """The old branch said "dispatched" for a file nobody read. A status that overstates what
    happened is how a queue goes unnoticed for months."""
    _status(state, False)
    status, result = TR.fleet_handoff("a goal", "j1", str(state))
    assert status == "awaiting_fleet"
    assert "waits" in result["note"]
    assert _commands(state) == {}, "nothing should be queued into a run that is not there"


def test_it_does_not_start_a_fleet_by_itself(state):
    """Spawning starts a browser, a set of workers and spends the tenant's Copilot budget.
    Doing that because a sentence arrived over a tunnel is a bigger act than queueing one."""
    assert TR.AUTOSTART is False, "autostart must be opt-in"
    _status(state, False)
    TR.fleet_handoff("a goal", "j1", str(state))
    assert _commands(state) == {}


def test_an_empty_goal_is_an_error_not_a_delivery(state):
    _status(state, True)
    status, _ = TR.fleet_handoff("   ", "j1", str(state))
    assert status == "error"
    assert _commands(state) == {}


# -- the whole path, through run_job ------------------------------------------------------------

def test_a_fleet_goal_job_reaches_the_running_fleet(state):
    """End to end: the job type an agent submits, through the router, into the run."""
    _status(state, True)
    job = {"id": "j9", "type": "fleet_goal", "payload": {"goal": "do the thing"}}
    rec = TR.run_job(job)
    assert rec["status"] == "dispatched"
    assert [a["text"] for a in _commands(state)["add_goal"]] == ["do the thing"]


def test_the_record_of_what_was_asked_for_is_still_written(state):
    """for_fleet/<id>.txt stays: it is the record of the goal, even now that delivery is real."""
    _status(state, True)
    TR.run_job({"id": "j9", "type": "fleet_goal", "payload": {"goal": "do the thing"}})
    with open(os.path.join(TR.TASKS, "for_fleet", "j9.txt"), encoding="utf-8") as fh:
        assert fh.read() == "do the thing"
