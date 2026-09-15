# -*- coding: utf-8 -*-
"""done/ records dispatch, not completion -- and until 2026-09-15 nothing ever corrected that.

WHAT HAPPENED. A sweep of .fleet/tasks/done/ found all 164 records reading
status="dispatched", ts_done=None -- including jobs that had been delivered into a fleet run
that was later stopped and never finished at all. relay/task_router.py's own module docstring
already warned that "done/ DOES NOT MEAN THE WORK IS DONE", but a careful reader still could
not answer "did job X finish, and how" from the queue: the outcome existed only in
relay/relay_fleet.py's RelayWorker.close(), which writes one line per finished worker to
<state_dir>/socket_route.jsonl (outcome, turns, reason -- the works), unconditionally, whether
or not the C# cockpit is open to archive it into .fleet/history.json. Nothing joined the two.

THE JOIN KEY THAT WAS MISSING. task_router.py already mints an admission id (`jid`) for every
fleet-bound goal and already threads it into the goal dict RelayWorker reads
(self.jid = goal.get("jid")) -- that id already reached history.json and the final sweep
snapshot (see add_goal_to_live_fleet's own comment), but the one line RelayWorker.close() logs
to socket_route.jsonl on its way out never carried it. Fixed at the write site:
relay/relay_fleet.py's `_socket_route().record("worker_done", ...)` call now passes
`jid=(self.jid or "")`. A row written before that fix has no jid and cannot be joined -- this
suite does not try to paper over that with a match on goal text, which is truncated to 600
chars in that ledger and duplicated across retries of the same goal.

WHAT THESE TESTS PIN. Three readings of one job id, corresponding to the three states
`job_status()` (relay/task_router.py) can now report for a fleet-bound job that used to read
"dispatched" no matter which of these was actually true:
  * a job whose worker actually finished reads "finished", with the real outcome attached;
  * a job dispatched into a run that ended (stopped, or simply no longer live) with no
    completion ever recorded does NOT read as finished -- it reads "unknown", an honest
    admission rather than a repeated, false "dispatched";
  * a job still an active worker in the CURRENT live run reads "in_flight" -- neither of the
    other two, because neither is true yet.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import relay.task_router as tr  # noqa: E402

importlib.reload(tr)


class Env:
    """Fresh, isolated .fleet-shaped tmp dir per test -- state_dir for socket_route.jsonl /
    status.json / the outcome cursor, and a separate tasks/ tree for done/, matching the
    pattern relay/test_fleet_landing_reconcile.py already uses for the sibling landing-ack
    reconcile pass this suite is the completion-side counterpart of."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="outcome_test_")
        self.prev_fsd = tr.FLEET_STATE_DIR
        self.prev_tasks = tr.TASKS
        tr.FLEET_STATE_DIR = self.tmp
        tr.TASKS = os.path.join(self.tmp, "tasks")
        tr.ensure_dirs()

    def close(self):
        tr.FLEET_STATE_DIR = self.prev_fsd
        tr.TASKS = self.prev_tasks
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers --------------------------------------------------------------------------

    def write_dispatched(self, jid, status="dispatched"):
        """A done/<jid>.json exactly as run_job/fleet_handoff leave one today: status says
        dispatched or awaiting_fleet, ts_done is None, and that is the whole record."""
        rec = {"id": jid, "type": "fleet_goal", "destination": "fleet", "ts_done": None,
               "status": status,
               "result": {"handoff": "for_fleet/%s.txt" % jid, "delivered": "add_goal",
                          "note": "queued into the running fleet"},
               "error": None}
        path = tr._p("done", "%s.json" % jid)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False)
        return path

    def append_worker_done(self, jid, status, outcome, turns=3, reason="", goal="a goal"):
        """One line of the ledger relay/relay_fleet.py's RelayWorker.close() writes, jid
        included -- exactly the shape the fix under test adds."""
        path = tr._socket_route_path(self.tmp)
        row = {"ts": time.time(), "at": "2026-09-15T00:00:00", "event": "worker_done",
               "worker": "w0", "goal": goal, "route": "socket", "fell_back": False,
               "turns": turns, "outcome": outcome, "status": status, "reason": reason,
               "jid": jid}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def status_json(self, running=True, workers=(), age_s=0):
        sp = os.path.join(self.tmp, "status.json")
        with open(sp, "w", encoding="utf-8") as fh:
            json.dump({"running": running, "workers": list(workers)}, fh)
        t = time.time() - age_s
        os.utime(sp, (t, t))

    def age_dispatch(self, jid, age_s):
        """Back-date done/<jid>.json's mtime, the same signal job_status() itself reads as
        "how long has this been waiting" when nothing else is known."""
        path = tr._p("done", "%s.json" % jid)
        t = time.time() - age_s
        os.utime(path, (t, t))


# ── 1. a job that finished reads as finished, with its outcome ────────────────────────────

def test_a_finished_job_reads_as_finished_with_its_outcome():
    e = Env()
    try:
        e.write_dispatched("jidDONE")
        e.append_worker_done("jidDONE", status="done", outcome="DONE", turns=5,
                             reason="", goal="do the thing")
        # No reconcile pass has run yet -- job_status()'s own live-scan fallback must find it.
        st = tr.job_status("jidDONE", state_dir=e.tmp)
        assert st["state"] == "finished", st
        assert st["status"] == "done", st
        assert st["result"]["outcome"] == "DONE", st
        assert st["result"]["turns"] == 5, st

        # And after a reconcile pass, the same answer comes from the written record instead.
        recs = tr._reconcile_outcomes(now_ts=1, state_dir=e.tmp)
        assert len(recs) == 1 and recs[0]["status"] == "done", recs
        assert os.path.isfile(tr._p("done", "jidDONE.outcome.json"))
        st2 = tr.job_status("jidDONE", state_dir=e.tmp)
        assert st2["state"] == "finished" and st2["status"] == "done", st2
        print("OK test_a_finished_job_reads_as_finished_with_its_outcome")
    finally:
        e.close()


def test_a_cancelled_job_reads_as_finished_cancelled_not_done():
    """A worker cancelled when its run was manually stopped IS a recorded outcome -- this is
    the case where the ledger already has the answer and the only bug was never reading it."""
    e = Env()
    try:
        e.write_dispatched("jidCANC")
        e.append_worker_done("jidCANC", status="cancelled", outcome="CANCELLED", turns=6,
                             reason="manually stopped, tab released")
        st = tr.job_status("jidCANC", state_dir=e.tmp)
        assert st["state"] == "finished", st
        assert st["status"] == "cancelled", st
        assert st["result"]["outcome"] == "CANCELLED", st
        print("OK test_a_cancelled_job_reads_as_finished_cancelled_not_done")
    finally:
        e.close()


# ── 2. a job delivered into a run that was stopped does not read as finished ──────────────

def test_a_job_whose_run_ended_with_no_outcome_does_not_read_as_finished_or_dispatched_forever():
    e = Env()
    try:
        e.write_dispatched("jidLOST")
        # No worker_done row was ever written for it (the run died / was stopped before the
        # worker got a chance to log one), and no fleet is live any more.
        e.status_json(running=False)
        e.age_dispatch("jidLOST", tr.JOB_STATUS_UNKNOWN_AFTER_S + 60)
        st = tr.job_status("jidLOST", state_dir=e.tmp)
        assert st["state"] != "finished", st
        assert st["state"] == "unknown", st
        assert "no completion was ever recorded" in st["detail"], st
        # And crucially: _reconcile_outcomes must not fabricate a completion for it either.
        recs = tr._reconcile_outcomes(now_ts=2, state_dir=e.tmp)
        assert recs == [], recs
        assert not os.path.isfile(tr._p("done", "jidLOST.outcome.json"))
        print("OK test_a_job_whose_run_ended_with_no_outcome_does_not_read_as_finished_or_dispatched_forever")
    finally:
        e.close()


def test_a_recently_dispatched_job_with_a_live_fleet_is_not_yet_called_unknown():
    """The "unknown" verdict is for a job that has waited long enough, or whose fleet is gone
    -- not for one that landed thirty seconds ago into a fleet that is still running. Calling
    that "unknown" too would just be a different word for the same premature claim."""
    e = Env()
    try:
        e.write_dispatched("jidFRESH")
        e.status_json(running=True, workers=[{"name": "w3", "jid": "someone-else"}])
        st = tr.job_status("jidFRESH", state_dir=e.tmp)
        assert st["state"] == "dispatched", st
        assert st["state"] not in ("finished", "unknown"), st
        print("OK test_a_recently_dispatched_job_with_a_live_fleet_is_not_yet_called_unknown")
    finally:
        e.close()


# ── 3. a job still in flight reads as neither finished nor lost ───────────────────────────

def test_a_job_still_an_active_worker_reads_as_in_flight():
    e = Env()
    try:
        e.write_dispatched("jidFLIGHT")
        e.status_json(running=True, workers=[{"name": "w0", "jid": "jidFLIGHT",
                                              "status": "waiting"}])
        st = tr.job_status("jidFLIGHT", state_dir=e.tmp)
        assert st["state"] == "in_flight", st
        assert st["state"] != "finished", st
        assert st["state"] != "unknown", st
        # Even long after JOB_STATUS_UNKNOWN_AFTER_S -- being an active worker overrides age.
        e.age_dispatch("jidFLIGHT", tr.JOB_STATUS_UNKNOWN_AFTER_S + 999)
        st2 = tr.job_status("jidFLIGHT", state_dir=e.tmp)
        assert st2["state"] == "in_flight", st2
        print("OK test_a_job_still_an_active_worker_reads_as_in_flight")
    finally:
        e.close()


# ── the fix at the source: no jid, no join, and this suite says so rather than guessing ────

def test_a_worker_done_row_with_no_jid_cannot_be_joined_and_is_not_guessed_at():
    """Pins the honest limitation directly: a ledger row written before the jid fix (or by
    a goal that never passed through admission) has no jid, _find_worker_outcome_by_jid can
    never match it to anything, and job_status() must not fall back to matching on goal text
    to compensate -- see the module docstring's account of why that would be unsafe."""
    e = Env()
    try:
        e.write_dispatched("jidNOMATCH")
        path = tr._socket_route_path(e.tmp)
        row = {"ts": time.time(), "at": "2026-09-15T00:00:00", "event": "worker_done",
               "worker": "w1", "goal": "the exact same goal text, dispatched twice",
               "route": "tab", "fell_back": False, "turns": 4, "outcome": "DONE",
               "status": "done", "reason": ""}          # no "jid" key at all
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        assert tr._find_worker_outcome_by_jid("jidNOMATCH", state_dir=e.tmp) is None
        e.status_json(running=False)
        e.age_dispatch("jidNOMATCH", tr.JOB_STATUS_UNKNOWN_AFTER_S + 1)
        st = tr.job_status("jidNOMATCH", state_dir=e.tmp)
        assert st["state"] == "unknown", st
        print("OK test_a_worker_done_row_with_no_jid_cannot_be_joined_and_is_not_guessed_at")
    finally:
        e.close()


# ── _reconcile_outcomes: cursor correctness, idempotency ──────────────────────────────────

def test_reconcile_outcomes_only_reads_new_lines_and_is_idempotent():
    e = Env()
    try:
        e.write_dispatched("jidA")
        e.write_dispatched("jidB")
        e.append_worker_done("jidA", status="done", outcome="DONE")
        first = tr._reconcile_outcomes(now_ts=1, state_dir=e.tmp)
        assert len(first) == 1 and first[0]["id"] == "jidA", first
        # Running again with nothing new appended finds nothing -- the cursor advanced.
        second = tr._reconcile_outcomes(now_ts=2, state_dir=e.tmp)
        assert second == [], second
        # A new line for the OTHER job is picked up on the next pass, and jidA is not
        # revisited (its outcome.json already exists).
        e.append_worker_done("jidB", status="error", outcome="ERROR")
        third = tr._reconcile_outcomes(now_ts=3, state_dir=e.tmp)
        assert len(third) == 1 and third[0]["id"] == "jidB", third
        assert tr.job_status("jidA", state_dir=e.tmp)["status"] == "done"
        assert tr.job_status("jidB", state_dir=e.tmp)["status"] == "error"
        print("OK test_reconcile_outcomes_only_reads_new_lines_and_is_idempotent")
    finally:
        e.close()


def test_job_status_of_a_non_fleet_job_passes_through_unchanged():
    """LOCAL jobs already resolve synchronously and are not this defect -- job_status must
    not reinvent their state machine."""
    e = Env()
    try:
        rec = {"id": "localX", "type": "shell", "destination": "local", "ts_done": 5,
               "status": "ok", "result": {"rc": 0, "output": "hi"}, "error": None}
        with open(tr._p("done", "localX.json"), "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        st = tr.job_status("localX", state_dir=e.tmp)
        assert st["state"] == "ok", st
        assert st["status"] == "ok", st
        print("OK test_job_status_of_a_non_fleet_job_passes_through_unchanged")
    finally:
        e.close()


def test_job_status_of_an_unknown_id_says_not_found():
    e = Env()
    try:
        st = tr.job_status("never-existed", state_dir=e.tmp)
        assert st["state"] == "not_found", st
        print("OK test_job_status_of_an_unknown_id_says_not_found")
    finally:
        e.close()


if __name__ == "__main__":
    test_a_finished_job_reads_as_finished_with_its_outcome()
    test_a_cancelled_job_reads_as_finished_cancelled_not_done()
    test_a_job_whose_run_ended_with_no_outcome_does_not_read_as_finished_or_dispatched_forever()
    test_a_recently_dispatched_job_with_a_live_fleet_is_not_yet_called_unknown()
    test_a_job_still_an_active_worker_reads_as_in_flight()
    test_a_worker_done_row_with_no_jid_cannot_be_joined_and_is_not_guessed_at()
    test_reconcile_outcomes_only_reads_new_lines_and_is_idempotent()
    test_job_status_of_a_non_fleet_job_passes_through_unchanged()
    test_job_status_of_an_unknown_id_says_not_found()
    print("ALL OUTCOME-RECONCILE TESTS PASSED")
