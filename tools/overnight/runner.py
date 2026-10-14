"""The unattended loop: wait for the start time, queue items, watch the fleet, then write the report.

Rules that make it safe to leave alone:
  * one item's failure (or one loop iteration's exception) never stops the batch;
  * nothing is killed: a time budget only changes what we RECORD (timeout), the fleet decides;
  * after the deadline, or when the disk gets close to the owner's floor, no new item is queued
    and the report is built from what exists;
  * the report is written whatever happened (report.finish never raises).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from tools.overnight import classify, state as S
from tools.overnight.common import Clock, JST, fmt_dt, from_epoch, to_epoch
from tools.overnight.detect import DONE, FAILED, QUEUED, RUNNING, UNSEEN, FleetReader
from tools.overnight.items import build_goal, Item
from tools.overnight.state import State, goal_hash

ANSWER_KEEP_CHARS = 20000
DEFAULT_MAX_MINUTES = 20.0
DEFAULT_MAX_INFLIGHT = 4
DEFAULT_POLL_S = 30
QUEUED_GRACE_FACTOR = 2.0
UNSEEN_GRACE_MIN = 20.0
MAX_CONSECUTIVE_LOOP_ERRORS = 50


class Runner:
    def __init__(self, st: State, clock=None, reader=None, submitter=None, log=None,
                 free_gb=None, floor_gb=None, awake=None, finish=None):
        self.st = st
        self.cfg = st.config
        self.clock = clock or Clock()
        self.reader = reader
        self.submitter = submitter
        self.log = log or (lambda m: None)
        self.free_gb = free_gb or (lambda: 1e9)
        self.floor_gb = floor_gb or (lambda: 0.0)
        self.awake = awake
        self.finish = finish
        self.out_dir = Path(self.cfg["out_dir"])
        self.stop_reason = ""

    # ---- time helpers
    def _now(self) -> datetime:
        return self.clock.now()

    def _deadline(self):
        d = self.cfg.get("deadline")
        return datetime.fromisoformat(d) if d else None

    def _past_deadline(self) -> bool:
        d = self._deadline()
        return d is not None and self._now() >= d

    def _budget_s(self, rec) -> float:
        mm = rec.get("max_minutes") or self.cfg.get("max_minutes", DEFAULT_MAX_MINUTES)
        return float(mm) * 60.0

    # ---- main
    def run(self) -> None:
        self.cfg.setdefault("run_started", fmt_dt(self._now()))
        self.cfg.setdefault("run_started_ts", to_epoch(self._now()))
        self.st.save()
        try:
            if self.awake is not None:
                with self.awake():
                    self._run_inner()
            else:
                self._run_inner()
        except Exception as e:
            self.log("batch stopped by an error: %r" % (e,))
            self.stop_reason = self.stop_reason or "error: %r" % (e,)
        except BaseException as e:  # Ctrl-C: the report is still written (finally), then it propagates
            self.log("batch interrupted: %r" % (e,))
            self.stop_reason = self.stop_reason or "interrupted"
            raise
        finally:
            self._close_out()

    def _run_inner(self) -> None:
        self._wait_for_start()
        self._recover_submitting()
        errors = 0
        while True:
            try:
                self._poll()
                reason = self._should_stop_submitting()
                if reason:
                    self.stop_reason = self.stop_reason or reason
                    self._skip_pending(reason)
                else:
                    self._submit_some()
                errors = 0
                if self._past_deadline():
                    self.log("deadline reached: stop waiting for running items")
                    return
                if self.st.all_terminal():
                    return
            except Exception as e:
                errors += 1
                self.log("loop error %d: %r" % (errors, e))
                if errors >= MAX_CONSECUTIVE_LOOP_ERRORS:
                    self.stop_reason = "too many internal errors"
                    return
            self.clock.sleep(self.cfg.get("poll_s", DEFAULT_POLL_S))

    def _wait_for_start(self) -> None:
        s = self.cfg.get("start_at")
        if not s:
            return
        start = datetime.fromisoformat(s)
        self.log("waiting until %s to start" % fmt_dt(start))
        while True:
            remaining = (start - self._now()).total_seconds()
            if remaining <= 0:
                break
            self.clock.sleep(min(30.0, remaining))
        self.log("start time reached")

    def _recover_submitting(self) -> None:
        """After a crash an item may be half-submitted: if the fleet has it, it was submitted."""
        for iid in self.st.by_status(S.SUBMITTING):
            rec = self.st.get(iid)
            seen = self._in_fleet(iid)
            if seen:
                self.st.set_status(iid, S.SUBMITTED, submitted_ts=to_epoch(self._now()))
                self.log("%s: found in the fleet after restart, treated as submitted" % iid)
            else:
                self.st.set_status(iid, S.PENDING)
                self.log("%s: not in the fleet after restart, will be submitted again" % iid)

    # ---- fleet access
    def _tag(self, iid: str) -> str:
        return "[%s]" % iid

    def _in_fleet(self, iid: str) -> bool:
        if self.reader is None:
            return False
        ob = self.reader.observe({iid: self._tag(iid)}, self.cfg.get("run_started_ts", 0) - 3600,
                                 to_epoch(self._now()))
        return ob[iid].state != UNSEEN

    # ---- stop conditions
    def _should_stop_submitting(self) -> str:
        if self._past_deadline():
            return "deadline"
        free, floor = self.free_gb(), self.floor_gb()
        if free < floor + self.cfg.get("floor_margin_gb", 0.5):
            return "disk free %.1f GB is near the floor %.1f GB" % (free, floor)
        return ""

    def _skip_pending(self, reason: str) -> None:
        for iid in self.st.by_status(S.PENDING):
            self.st.set_status(iid, S.SKIPPED, error="not queued: " + reason,
                               finished_ts=to_epoch(self._now()))
            self.log("%s: skipped (%s)" % (iid, reason))

    # ---- submitting
    def _inflight(self) -> int:
        return len(self.st.by_status(S.SUBMITTING, S.SUBMITTED, S.RUNNING))

    def _submit_some(self) -> None:
        while self._inflight() < self.cfg.get("max_inflight", DEFAULT_MAX_INFLIGHT):
            pend = self.st.by_status(S.PENDING)
            if not pend or self._past_deadline():
                return
            self._submit_one(pend[0])

    def _goal_for(self, rec, variant: int) -> str:
        item = Item(id=rec["id"], ask=rec["ask"], tags=rec["tags"], max_minutes=rec["max_minutes"])
        return build_goal(item, self.out_dir, self.cfg.get("preamble", ""), variant + int(rec.get("variant_base", 0)))

    def _submit_one(self, iid: str) -> None:
        rec = self.st.get(iid)
        try:
            goal = self._goal_for(rec, 0)
        except ValueError as e:
            self.st.set_status(iid, S.SKIPPED, error=str(e), category=classify.OTHER,
                               finished_ts=to_epoch(self._now()))
            return
        if self.st.already_submitted(iid, goal):
            self.log("%s: already submitted with this goal, not sent again" % iid)
            return
        (self.out_dir / iid).mkdir(parents=True, exist_ok=True)
        variant = 0
        while True:
            self.st.set_status(iid, S.SUBMITTING, goal=goal, goal_hash=goal_hash(goal),
                               variant=variant, attempts=rec["attempts"] + 1)
            res = self.submitter.submit(goal, probe=lambda i=iid: self._in_fleet(i))
            if res.ok:
                self.st.set_status(iid, S.SUBMITTED, submitted_ts=to_epoch(self._now()),
                                   result_dir=str(self.out_dir / iid),
                                   job_id=getattr(res, "job_id", "") or "")
                self.log("%s: submitted (%d attempt(s)); cockpit: %s"
                         % (iid, res.attempts, self.submitter.readonly_check()))
                return
            if res.duplicate and variant == 0:
                variant = 1
                goal = self._goal_for(rec, variant)
                self.log("%s: rejected as a duplicate, asking again in other words" % iid)
                continue
            if res.duplicate:
                self.st.set_status(iid, S.SKIPPED, error="重複で拒否 (fleet duplicate rule)",
                                   category=classify.OTHER, finished_ts=to_epoch(self._now()))
            else:
                self.st.set_status(iid, S.FAILED, error="投入失敗: " + res.output[-300:],
                                   category=classify.NOT_SUBMITTABLE, finished_ts=to_epoch(self._now()))
            self.log("%s: not queued (%s)" % (iid, rec["error"][:120]))
            return

    # ---- watching
    def _poll(self) -> None:
        active = self.st.by_status(S.SUBMITTED, S.RUNNING)
        if not active or self.reader is None:
            return
        now = self._now()
        now_ts = to_epoch(now)
        obs = self.reader.observe({i: self._tag(i) for i in active},
                                  self.cfg.get("run_started_ts", 0), now_ts)
        for iid in active:
            rec = self.st.get(iid)
            ob = obs.get(iid)
            try:
                self._apply(iid, rec, ob, now_ts)
            except Exception as e:
                self.log("%s: could not update from the fleet: %r" % (iid, e))

    def _apply(self, iid, rec, ob, now_ts) -> None:
        if ob is not None and ob.state in (DONE, FAILED):
            self._finish_item(iid, rec, ob, now_ts)
            return
        if ob is not None and ob.state == RUNNING and rec["status"] == S.SUBMITTED:
            self.st.set_status(iid, S.RUNNING, started_ts=ob.started_ts or now_ts,
                               worker=ob.worker)
        elif ob is not None and ob.state == RUNNING and ob.started_ts and not rec["started_ts"]:
            self.st.update(iid, started_ts=ob.started_ts)
        if ob is not None and ob.state == UNSEEN and rec["status"] == S.SUBMITTED \
                and not self._job_waiting(rec):
            waited = now_ts - (rec["submitted_ts"] or now_ts)
            if waited > self.cfg.get("unseen_grace_min", UNSEEN_GRACE_MIN) * 60:
                self.st.set_status(iid, S.FAILED, finished_ts=now_ts, category=classify.NOT_SUBMITTABLE,
                                   error="the fleet never showed this goal after %d min" % (waited // 60))
                self.log("%s: never appeared in the fleet" % iid)
                return
        # time budget: counted from when work started, or from submission with a queue allowance
        budget = self._budget_s(rec)
        rec = self.st.get(iid)
        if rec["started_ts"]:
            over = now_ts - rec["started_ts"] > budget
        else:
            over = now_ts - (rec["submitted_ts"] or now_ts) > budget * QUEUED_GRACE_FACTOR
        if over:
            part = ob.answer if ob is not None else ""
            self._store_answer(iid, part)
            self.st.set_status(iid, S.TIMEOUT, finished_ts=now_ts, category=classify.TIMED_OUT,
                               error="time budget %d min exceeded; the worker was not stopped"
                                     % (budget // 60))
            self.log("%s: timeout recorded" % iid)

    def _job_waiting(self, rec) -> bool:
        """Queue route: the job file is still in the fleet's queue directories (not yet picked up)."""
        jid = rec.get("job_id") or ""
        waiting = getattr(self.reader, "job_waiting", None)
        try:
            return bool(jid and waiting and waiting(jid))
        except Exception:
            return False

    def _store_answer(self, iid: str, answer: str) -> None:
        answer = answer or ""
        if len(answer) > ANSWER_KEEP_CHARS:
            try:
                d = self.out_dir / iid
                d.mkdir(parents=True, exist_ok=True)
                (d / "answer_full.txt").write_text(answer, encoding="utf-8")
            except OSError:
                pass
            answer = answer[:ANSWER_KEEP_CHARS] + "\n... (truncated; see answer_full.txt)"
        self.st.update(iid, answer=answer)

    def _finish_item(self, iid, rec, ob, now_ts) -> None:
        self._store_answer(iid, ob.answer)
        fields = dict(outcome=ob.outcome, reason=ob.reason, worker=ob.worker or rec["worker"],
                      turns=ob.turns, answer_source=ob.source,
                      started_ts=rec["started_ts"] or ob.started_ts,
                      finished_ts=ob.finished_ts or now_ts)
        if ob.state == DONE:
            note = classify.suspicious_success(ob.answer)
            if note:
                fields["note"] = "回答の末尾に失敗らしい記述 (%s)。内容を確認してください。" % note
            self.st.set_status(iid, S.DONE, **fields)
            self.log("%s: done (%s)" % (iid, ob.outcome))
        else:
            fields["category"] = classify.classify_failure(ob.answer, ob.outcome, "", ob.reason)
            fields["error"] = "outcome=%s %s" % (ob.outcome, ob.reason)
            self.st.set_status(iid, S.FAILED, **fields)
            self.log("%s: failed (%s, %s)" % (iid, ob.outcome, fields["category"]))

    # ---- end
    def _close_out(self) -> None:
        now_ts = to_epoch(self._now())
        reason = self.stop_reason or "finished"
        for iid in self.st.ids():
            rec = self.st.get(iid)
            if rec["status"] in S.TERMINAL:
                continue
            if rec["status"] in (S.SUBMITTED, S.RUNNING):
                try:
                    self.st.set_status(iid, S.TIMEOUT, finished_ts=now_ts, category=classify.TIMED_OUT,
                                       error="batch ended (%s) before this finished; the worker was not stopped" % reason)
                except Exception:
                    pass
            else:
                try:
                    self.st.set_status(iid, S.SKIPPED, finished_ts=now_ts,
                                       error="not run (%s)" % reason)
                except Exception:
                    pass
        recs = [self.st.get(i) for i in self.st.ids()]
        runnable = [r for r in recs if r["status"] != S.SKIPPED or r.get("attempts")]
        if not self.stop_reason and runnable and not any(r["status"] == S.DONE for r in recs):
            blocked = sum(1 for r in recs if r.get("category") == classify.NOT_SUBMITTABLE)
            reason = ("no item succeeded: %d of %d could not be submitted" % (blocked, len(recs))
                      if blocked else "no item succeeded")
        self.cfg["run_ended"] = fmt_dt(self._now())
        self.cfg["stop_reason"] = reason
        try:
            self.st.save()
        except Exception as e:
            self.log("could not save state: %r" % (e,))
        if self.finish is not None:
            self.finish(self.st)
