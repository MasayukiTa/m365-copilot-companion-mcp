"""Fleet-absent goals must WAIT, not vanish -- the behaviour goal (a) and (b) require.

The live defect: add_goal_to_live_fleet trusts fleet_is_live() (a stale status.json
mtime + a running flag), writes the command, and records "delivered". When the fleet
dies in the window between the optimistic check and the read, the command is never
consumed and the goal is gone -- recorded delivered, waiting nowhere.

The fix these tests pin drives the REAL sender/reconciler against a shared tmp state:

  (a) fleet absent -> no ack lands within the grace window -> the goal is requeued to
      for_fleet/ (visible as waiting) and the record says awaiting_fleet, not delivered.
  (b) a real reader writes the ack -> reconcile confirms the landing, clears the marker,
      records it landed, and does NOT requeue.

A third case fixes the grace boundary itself: a fresh marker inside the grace window is
left alone, so a merely-slow fleet is not prematurely requeued.

Portable on purpose: no hard-coded path, no importlib.reload, no sys.path surgery, no
Windows-only assumption -- so it runs on the Linux CI runner the manifest check guards.
"""
import json
import os
import time

import pytest

from relay import task_router as tr
from relay import fleet_runner as fr


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """Redirect the ack root and the tasks tree into tmp; restore is automatic."""
    state = tmp_path / "state"
    tasks = tmp_path / "tasks"
    monkeypatch.setattr(tr, "FLEET_STATE_DIR", str(state))
    monkeypatch.setattr(tr, "TASKS", str(tasks))
    tr.ensure_dirs()

    def write_status(running=True, age_s=0):
        sp = state / "status.json"
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps({"running": running}), encoding="utf-8")
        t = time.time() - age_s
        os.utime(sp, (t, t))

    return type("F", (), {"state": str(state), "tasks": str(tasks),
                          "write_status": staticmethod(write_status)})


def _p(*parts):
    return os.path.join(*parts)


def test_fleet_absent_goal_is_requeued_not_lost(fleet):
    """Goal (a): the vanishing-window goal ends up WAITING in for_fleet, not gone."""
    fleet.write_status(running=True, age_s=0)   # looks live; no reader will ever run
    st, _res = tr.fleet_handoff("GOAL-lost", "jidLOST", state_dir=fleet.state)
    assert st == "dispatched"

    marker = _p(tr.TASKS, "awaiting_ack", "jidLOST.json")
    assert os.path.isfile(marker), "handoff must leave an awaiting-ack marker"
    assert not tr.fleet_landing_confirmed("jidLOST", state_dir=fleet.state)

    # Inside the grace window nothing is requeued yet.
    assert tr._reconcile_landings(now_ts=None, state_dir=fleet.state) == []
    assert os.path.isfile(marker)

    # Age the marker past the grace window the reconcile pass reads.
    m = json.loads(open(marker, encoding="utf-8").read())
    m["ts"] = time.time() - (tr.RECONCILE_ACK_GRACE_S + 5)
    open(marker, "w", encoding="utf-8").write(json.dumps(m))

    out = tr._reconcile_landings(now_ts=42, state_dir=fleet.state)
    assert len(out) == 1 and out[0]["status"] == "awaiting_fleet", out
    assert out[0]["result"]["requeued"] is True
    # requeued to for_fleet (waiting, visible), marker gone, reconcile recorded
    assert os.path.isfile(_p(tr.TASKS, "for_fleet", "jidLOST.txt"))
    assert not os.path.exists(marker)
    assert os.path.isfile(_p(tr.TASKS, "done", "jidLOST.reconcile-requeued.json"))


def test_real_landing_is_confirmed_only_when_the_reader_acks(fleet):
    """Goal (b): delivered is recorded ONLY after a real reader leaves the ack."""
    fleet.write_status(running=True, age_s=0)
    st, _res = tr.fleet_handoff("GOAL-ok", "jidOK", state_dir=fleet.state)
    assert st == "dispatched"
    assert os.path.isfile(_p(tr.TASKS, "awaiting_ack", "jidOK.json"))

    # a real fleet consumes its command channel -> writes the ack receipt
    cmds = fr.read_commands(fleet.state)
    assert any("add_goal" in c for c in cmds), cmds
    assert tr.fleet_landing_confirmed("jidOK", state_dir=fleet.state)

    out = tr._reconcile_landings(now_ts=9, state_dir=fleet.state)
    assert len(out) == 1 and out[0]["status"] == "dispatched"
    assert out[0]["result"]["landing_confirmed"] is True
    assert not os.path.exists(_p(tr.TASKS, "awaiting_ack", "jidOK.json"))
    assert os.path.isfile(_p(tr.TASKS, "done", "jidOK.landed.json"))
    assert not os.path.exists(_p(tr.TASKS, "for_fleet", "jidOK.txt")), "a landed goal must not be requeued"


def test_within_grace_a_slow_fleet_is_left_alone(fleet):
    """A fresh marker inside the grace window is not prematurely requeued."""
    fleet.write_status(running=True, age_s=0)
    tr.fleet_handoff("GOAL-slow", "jidSLOW", state_dir=fleet.state)
    assert tr._reconcile_landings(now_ts=0, state_dir=fleet.state) == []
    assert os.path.isfile(_p(tr.TASKS, "awaiting_ack", "jidSLOW.json"))
    assert not os.path.exists(_p(tr.TASKS, "for_fleet", "jidSLOW.txt"))
