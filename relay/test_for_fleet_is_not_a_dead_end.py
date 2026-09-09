"""_deliver_waiting_goals must look at for_fleet/ when NOTHING is live -- that is the case.

The function used to open with `if not fleet_is_live(...): return out`, so the one routine
whose job is to deliver goals that arrived while no fleet was running gave up precisely when
no fleet was running. A goal parked by _reconcile_landings' requeue path is written straight
into for_fleet/ without passing through fleet_handoff, so it could never reach autostart
again: measured 2026-09-09, one goal sat there while the supervisor ran this pass every 15s
for 47 minutes and logged nothing -- silence, not an error.

These tests pin the two halves of the fix together, because either alone is a defect:
the cold path must be TRIED (or the dead end returns), and it must be tried AT MOST ONCE per
pass (or N waiting files start N fleets, trading a stall for an amplification).
"""
import importlib
import json
import os
import shutil
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import relay.task_router as tr  # noqa: E402
importlib.reload(tr)


class Env:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="for_fleet_deadend_")
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

    def park(self, jid, goal):
        """Write straight into for_fleet/, exactly as _reconcile_landings' requeue does."""
        with open(os.path.join(tr.TASKS, "for_fleet", "%s.txt" % jid), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write(goal)

    def close(self):
        tr.FLEET_STATE_DIR = self.prev_fsd
        tr.TASKS = self.prev_tasks
        shutil.rmtree(self.tmp, ignore_errors=True)


def _record_handoffs(monkeypatch, status="awaiting_fleet"):
    """Replace fleet_handoff with a recorder. Nothing here may start a real fleet."""
    seen = []

    def _fake(goal, jid, state_dir=None):
        seen.append(jid)
        return status, {"handoff": "for_fleet/%s.txt" % jid, "note": "test"}

    monkeypatch.setattr(tr, "fleet_handoff", _fake)
    return seen


def test_a_parked_goal_is_offered_even_though_no_fleet_is_live(monkeypatch):
    """The regression itself. Nothing live is the CONDITION for delivery, not a reason to skip."""
    e = Env()
    try:
        e.status(running=False, age_s=9999)      # unambiguously not live
        assert tr.fleet_is_live(e.tmp) is False
        e.park("jidCOLD", "GOAL-parked-while-cold")
        seen = _record_handoffs(monkeypatch)
        tr._deliver_waiting_goals(now_ts=42, state_dir=e.tmp)
        assert seen == ["jidCOLD"], (
            "for_fleet/ was not even looked at with nothing live -- the dead end is back")
    finally:
        e.close()


def test_a_cold_pass_offers_exactly_one_goal_however_many_are_waiting(monkeypatch):
    """A launch takes seconds to read as live, so every file in the same pass would still see
    "nothing in flight". Without this bound, three parked goals mean three fleets."""
    e = Env()
    try:
        e.status(running=False, age_s=9999)
        for jid in ("jidA", "jidB", "jidC"):
            e.park(jid, "GOAL-" + jid)
        seen = _record_handoffs(monkeypatch)
        tr._deliver_waiting_goals(now_ts=42, state_dir=e.tmp)
        assert len(seen) == 1, "one cold start per pass, got %d: %r" % (len(seen), seen)
    finally:
        e.close()


def test_a_live_pass_still_delivers_the_whole_backlog(monkeypatch):
    """The one-per-pass bound applies ONLY to the cold path. Once a fleet is up, the backlog
    must drain in a single pass -- otherwise the fix converts a dead end into a slow leak."""
    e = Env()
    try:
        e.status(running=True, age_s=0)
        assert tr.fleet_is_live(e.tmp) is True
        for jid in ("jidA", "jidB", "jidC"):
            e.park(jid, "GOAL-" + jid)
        seen = _record_handoffs(monkeypatch, status="dispatched")
        tr._deliver_waiting_goals(now_ts=42, state_dir=e.tmp)
        assert sorted(seen) == ["jidA", "jidB", "jidC"], seen
        # delivered goals are removed only after a "dispatched" result
        left = os.listdir(os.path.join(tr.TASKS, "for_fleet"))
        assert left == [], left
    finally:
        e.close()


def test_an_undelivered_goal_is_kept_not_dropped(monkeypatch):
    """A cold offer that does not dispatch must leave the file where it is. Losing it here
    would look exactly like a goal that was never submitted."""
    e = Env()
    try:
        e.status(running=False, age_s=9999)
        e.park("jidKEEP", "GOAL-keep-me")
        _record_handoffs(monkeypatch, status="awaiting_fleet")
        tr._deliver_waiting_goals(now_ts=42, state_dir=e.tmp)
        assert os.path.isfile(os.path.join(tr.TASKS, "for_fleet", "jidKEEP.txt"))
    finally:
        e.close()
