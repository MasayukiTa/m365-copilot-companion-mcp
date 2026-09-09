
import os, sys, json, time, tempfile, shutil, importlib

# Computed, never written down. The literal that used to sit here carried the owner's
# home path into a PUBLIC repository, and a second line then overwrote the computed
# value with it -- so the correct expression was already present and being discarded.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


# ── 2026-09-09: the plan's literal wording ("exactly once", "RUNNING/DONE never
# requeued") was only an implicit consequence of the marker-delete design -- these two pin
# it directly, per codex-plan item 2's own stated evidence bar (docs/research/
# _codex_plan_unfinished_20260908.md), rather than leaving it to hold by accident. ──

def test_a_reconciled_goal_is_not_requeued_a_second_time():
    """The ENTIRE guarantee rests on one line: os.remove(mpath) after the requeue. This pins
    the observable behaviour that line exists to produce, not the line itself -- a rewrite
    that kept the marker around some other way would still have to pass this."""
    e = Env()
    try:
        e.status(running=True, age_s=0)
        tr.fleet_handoff("GOAL-lost", "jidTWICE", state_dir=e.tmp)
        mp = os.path.join(tr.TASKS, "awaiting_ack", "jidTWICE.json")
        m = json.load(open(mp, encoding="utf-8"))
        m["ts"] = time.time() - (tr.RECONCILE_ACK_GRACE_S + 5)
        json.dump(m, open(mp, "w", encoding="utf-8"))

        first = tr._reconcile_landings(now_ts=1, state_dir=e.tmp)
        assert len(first) == 1 and first[0]["result"]["requeued"] is True, first
        for_fleet = os.path.join(tr.TASKS, "for_fleet", "jidTWICE.txt")
        assert os.path.isfile(for_fleet)
        first_mtime = os.path.getmtime(for_fleet)

        # The marker is gone -- nothing left in awaiting_ack/ to reconcile a second time.
        assert not os.path.exists(mp)
        second = tr._reconcile_landings(now_ts=2, state_dir=e.tmp)
        assert second == [], (
            "a goal already reconciled once was reconciled again: %r" % (second,))
        assert os.path.getmtime(for_fleet) == first_mtime, (
            "the for_fleet waiter was rewritten by a second reconcile pass")
        print("OK test_a_reconciled_goal_is_not_requeued_a_second_time")
    finally:
        e.close()


def test_a_landed_goal_is_never_requeued_even_long_past_grace():
    """The plan's own wording is 'RUNNING/DONE goals are never requeued'. This mechanism does
    not read a status field to tell those apart -- it reads whether the fleet's ack landed,
    which is true the instant the goal is picked up and stays true for as long as it runs AND
    after it finishes. So one test covering 'ack landed, arbitrarily old marker' covers both
    named states at once: there is no age past which a landed goal starts looking lost."""
    e = Env()
    try:
        e.status(running=True, age_s=0)
        tr.fleet_handoff("GOAL-running-or-done", "jidLANDED", state_dir=e.tmp)
        # the fleet reads its command channel -> ack lands, exactly as a real running (or by
        # now finished) worker would have produced
        cmds = fr.read_commands(e.tmp)
        assert any("add_goal" in c for c in cmds), cmds
        assert tr.fleet_landing_confirmed("jidLANDED", state_dir=e.tmp)
        # age the marker WAY past grace -- if age alone drove the decision this would requeue
        mp = os.path.join(tr.TASKS, "awaiting_ack", "jidLANDED.json")
        m = json.load(open(mp, encoding="utf-8"))
        m["ts"] = time.time() - (tr.RECONCILE_ACK_GRACE_S * 50)
        json.dump(m, open(mp, "w", encoding="utf-8"))

        out = tr._reconcile_landings(now_ts=3, state_dir=e.tmp)
        assert len(out) == 1 and out[0]["status"] == "dispatched", out
        assert out[0]["result"]["landing_confirmed"] is True
        assert not os.path.exists(os.path.join(tr.TASKS, "for_fleet", "jidLANDED.txt")), (
            "a goal whose ack already landed was requeued anyway")
        print("OK test_a_landed_goal_is_never_requeued_even_long_past_grace")
    finally:
        e.close()


if __name__ == "__main__":
    test_vanishing_window_goal_is_requeued_by_reconcile()
    test_real_landing_is_confirmed_and_marker_cleared()
    test_within_grace_no_ack_is_left_alone()
    test_a_reconciled_goal_is_not_requeued_a_second_time()
    test_a_landed_goal_is_never_requeued_even_long_past_grace()
    print("ALL RECONCILE TESTS PASSED")
