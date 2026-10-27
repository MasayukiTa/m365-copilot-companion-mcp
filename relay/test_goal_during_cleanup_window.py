# -*- coding: utf-8 -*-
"""A goal submitted during a fleet's post-run cleanup must not be add_goal'd into a channel
that will never be drained again.

fleet_runner reads the add_goal channel only from inside run_relay_fleet (on_tick ->
_drain_commands). The instant that returns, no goal is read -- yet status.json still says
running:True with a fresh mtime for the whole cleanup that follows (measured ~2 min). Through
that window fleet_is_live() returned True, so fleet_handoff add_goal'd the goal and filed it
"dispatched"; nothing read it, and the 90s ack grace sat on it before re-queueing.

THE FIX. fleet_runner stamps status.json accepting_goals:False the moment the sweep loop exits.
fleet_accepting_goals() reads it, fleet_handoff routes such a goal to autostart instead, and
_reconcile_landings cuts the ack grace short once the run has stopped reading. An old status
without the stamp behaves exactly as before.
"""
import os, sys, json, time, tempfile, shutil, importlib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO); sys.path.insert(0, os.path.join(REPO, "relay"))

import relay.task_router as tr
import relay.fleet_runner as fr
importlib.reload(tr); importlib.reload(fr)

_OMIT = object()


class Env:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="cleanup_goal_test_")
        self.prev_fsd = tr.FLEET_STATE_DIR
        self.prev_tasks = tr.TASKS
        self.prev_autostart = tr.AUTOSTART
        tr.FLEET_STATE_DIR = self.tmp
        tr.TASKS = os.path.join(self.tmp, "tasks")
        tr.AUTOSTART = False
        tr.ensure_dirs()

    def status(self, running=True, age_s=0, accepting=_OMIT):
        sp = os.path.join(self.tmp, "status.json")
        snap = {"running": running}
        if accepting is not _OMIT:
            snap["accepting_goals"] = accepting
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(snap, f)
        t = time.time() - age_s
        os.utime(sp, (t, t))

    def close(self):
        tr.FLEET_STATE_DIR = self.prev_fsd
        tr.TASKS = self.prev_tasks
        tr.AUTOSTART = self.prev_autostart
        shutil.rmtree(self.tmp, ignore_errors=True)


def _marker(jid):
    return os.path.join(tr.TASKS, "awaiting_ack", "%s.json" % jid)


def test_accepting_true_unless_explicitly_false():
    e = Env()
    try:
        e.status(accepting=_OMIT)
        assert tr.fleet_accepting_goals(e.tmp) is True
        e.status(accepting=True)
        assert tr.fleet_accepting_goals(e.tmp) is True
        e.status(accepting=False)
        assert tr.fleet_accepting_goals(e.tmp) is False
        e.status(age_s=tr.FLEET_LIVE_MAX_AGE_S + 5, accepting=False)
        assert tr.fleet_accepting_goals(e.tmp) is True, "a stale stamp is not read"
        print("OK test_accepting_true_unless_explicitly_false")
    finally:
        e.close()


def test_goal_during_cleanup_is_not_add_goaled():
    e = Env()
    try:
        e.status(running=True, age_s=0, accepting=False)
        st, res = tr.fleet_handoff("cleanup goal", "jCLEAN", state_dir=e.tmp)
        assert st == "awaiting_fleet", (st, res)
        assert res.get("delivered") != "add_goal", res
        assert not os.path.isfile(_marker("jCLEAN"))
        cmds = fr.read_commands(e.tmp)
        assert not any("add_goal" in c for c in cmds), cmds
        assert os.path.isfile(os.path.join(tr.TASKS, "for_fleet", "jCLEAN.txt"))
        print("OK test_goal_during_cleanup_is_not_add_goaled")
    finally:
        e.close()


def test_live_accepting_fleet_still_joins():
    e = Env()
    try:
        e.status(running=True, age_s=0, accepting=_OMIT)
        st, res = tr.fleet_handoff("live goal", "jLIVE", state_dir=e.tmp)
        assert st == "dispatched", (st, res)
        assert res.get("delivered") == "add_goal", res
        assert os.path.isfile(_marker("jLIVE"))
        cmds = fr.read_commands(e.tmp)
        assert any("add_goal" in c for c in cmds), cmds
        print("OK test_live_accepting_fleet_still_joins")
    finally:
        e.close()


def test_reconcile_requeues_inside_grace_when_stopped_reading():
    e = Env()
    try:
        e.status(running=True, age_s=0, accepting=True)
        st, _ = tr.fleet_handoff("stops before read", "jSTOP", state_dir=e.tmp)
        assert st == "dispatched", st
        assert os.path.isfile(_marker("jSTOP"))
        e.status(running=True, age_s=0, accepting=False)
        out = tr._reconcile_landings(now_ts=1, state_dir=e.tmp)
        assert len(out) == 1 and out[0]["status"] == "awaiting_fleet", out
        assert out[0]["result"]["requeued"] is True
        assert os.path.isfile(os.path.join(tr.TASKS, "for_fleet", "jSTOP.txt"))
        assert not os.path.exists(_marker("jSTOP"))
        print("OK test_reconcile_requeues_inside_grace_when_stopped_reading")
    finally:
        e.close()


def test_reconcile_waits_grace_for_live_accepting_fleet():
    e = Env()
    try:
        e.status(running=True, age_s=0, accepting=True)
        st, _ = tr.fleet_handoff("may still be read", "jWAIT", state_dir=e.tmp)
        assert st == "dispatched", st
        out = tr._reconcile_landings(now_ts=1, state_dir=e.tmp)
        assert out == [], out
        assert os.path.isfile(_marker("jWAIT"))
        assert not os.path.isfile(os.path.join(tr.TASKS, "for_fleet", "jWAIT.txt"))
        print("OK test_reconcile_waits_grace_for_live_accepting_fleet")
    finally:
        e.close()


if __name__ == "__main__":
    test_accepting_true_unless_explicitly_false()
    test_goal_during_cleanup_is_not_add_goaled()
    test_live_accepting_fleet_still_joins()
    test_reconcile_requeues_inside_grace_when_stopped_reading()
    test_reconcile_waits_grace_for_live_accepting_fleet()
    print("all ok")
