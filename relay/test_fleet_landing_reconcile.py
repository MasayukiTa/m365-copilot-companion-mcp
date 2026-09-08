
import os, sys, json, time, tempfile, shutil, importlib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if "__file__" in globals() else "C:/Users/M118A8586/resonac-mcp"
REPO = "C:/Users/M118A8586/resonac-mcp"
sys.path.insert(0, REPO); sys.path.insert(0, os.path.join(REPO, "relay"))

import relay.task_router as tr
import relay.fleet_runner as fr
importlib.reload(tr); importlib.reload(fr)


class Env:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="reconcile_test_")
        # point both the ack root (FLEET_STATE_DIR) and the tasks tree (TASKS) into tmp
        self.prev_fsd = tr.FLEET_STATE_DIR
        self.prev_tasks = tr.TASKS
        tr.FLEET_STATE_DIR = self.tmp
        tr.TASKS = os.path.join(self.tmp, "tasks")
        tr.ensure_dirs()
    def status(self, running=True, age_s=0):
        sp = os.path.join(self.tmp, "status.json")
        with open(sp, "w", encoding="utf-8") as f:
            json.dump({"running": running}, f)
        t = time.time() - age_s
        os.utime(sp, (t, t))
    def close(self):
        tr.FLEET_STATE_DIR = self.prev_fsd
        tr.TASKS = self.prev_tasks
        shutil.rmtree(self.tmp, ignore_errors=True)


def test_vanishing_window_goal_is_requeued_by_reconcile():
    e = Env()
    try:
        e.status(running=True, age_s=0)          # looks live, but no reader will run
        st, res = tr.fleet_handoff("GOAL-lost", "jidLOST", state_dir=e.tmp)
        assert st == "dispatched", st
        # marker recorded, no ack yet
        assert os.path.isfile(os.path.join(tr.TASKS, "awaiting_ack", "jidLOST.json"))
        assert not tr.fleet_landing_confirmed("jidLOST", state_dir=e.tmp)
        # within grace (real wall clock, marker ts is fresh): nothing happens
        out = tr._reconcile_landings(now_ts=None, state_dir=e.tmp)
        assert out == [], out
        assert os.path.isfile(os.path.join(tr.TASKS, "awaiting_ack", "jidLOST.json"))
        # age the marker beyond grace on the wall clock the pass actually reads
        mp = os.path.join(tr.TASKS, "awaiting_ack", "jidLOST.json")
        m = json.load(open(mp, encoding="utf-8"))
        m["ts"] = time.time() - (tr.RECONCILE_ACK_GRACE_S + 5)
        json.dump(m, open(mp, "w", encoding="utf-8"))
        out = tr._reconcile_landings(now_ts=42, state_dir=e.tmp)
        assert len(out) == 1 and out[0]["status"] == "awaiting_fleet", out
        assert out[0]["result"]["requeued"] is True
        # goal re-queued to for_fleet, marker gone, reconcile record written
        assert os.path.isfile(os.path.join(tr.TASKS, "for_fleet", "jidLOST.txt"))
        assert not os.path.exists(mp)
        assert os.path.isfile(os.path.join(tr.TASKS, "done", "jidLOST.reconcile-requeued.json"))
        print("OK test_vanishing_window_goal_is_requeued_by_reconcile")
    finally:
        e.close()


def test_real_landing_is_confirmed_and_marker_cleared():
    e = Env()
    try:
        e.status(running=True, age_s=0)
        st, res = tr.fleet_handoff("GOAL-ok", "jidOK", state_dir=e.tmp)
        assert st == "dispatched"
        assert os.path.isfile(os.path.join(tr.TASKS, "awaiting_ack", "jidOK.json"))
        # a real fleet reads its command channel -> writes the ack receipt
        cmds = fr.read_commands(e.tmp)
        assert any("add_goal" in c for c in cmds), cmds
        assert tr.fleet_landing_confirmed("jidOK", state_dir=e.tmp)
        out = tr._reconcile_landings(now_ts=9, state_dir=e.tmp)
        assert len(out) == 1 and out[0]["status"] == "dispatched"
        assert out[0]["result"]["landing_confirmed"] is True
        assert not os.path.exists(os.path.join(tr.TASKS, "awaiting_ack", "jidOK.json"))
        assert os.path.isfile(os.path.join(tr.TASKS, "done", "jidOK.landed.json"))
        # not re-queued
        assert not os.path.exists(os.path.join(tr.TASKS, "for_fleet", "jidOK.txt"))
        print("OK test_real_landing_is_confirmed_and_marker_cleared")
    finally:
        e.close()


def test_within_grace_no_ack_is_left_alone():
    e = Env()
    try:
        e.status(running=True, age_s=0)
        tr.fleet_handoff("GOAL-slow", "jidSLOW", state_dir=e.tmp)
        out = tr._reconcile_landings(now_ts=0, state_dir=e.tmp)
        assert out == []
        assert os.path.isfile(os.path.join(tr.TASKS, "awaiting_ack", "jidSLOW.json"))
        assert not os.path.exists(os.path.join(tr.TASKS, "for_fleet", "jidSLOW.txt"))
        print("OK test_within_grace_no_ack_is_left_alone")
    finally:
        e.close()


if __name__ == "__main__":
    test_vanishing_window_goal_is_requeued_by_reconcile()
    test_real_landing_is_confirmed_and_marker_cleared()
    test_within_grace_no_ack_is_left_alone()
    print("ALL RECONCILE TESTS PASSED")
