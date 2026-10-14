"""Read-only view of the fleet's own state: which of OUR items finished, what did they answer.

Sources (all under the fleet state directory, usually <repo>/.fleet; nothing is written):
  status.json              live snapshot: workers[] with goal, status, outcome, last, phase_events
  tasks/done/<id>.outcome.json   final outcome, turns, reason, worker, ts_done
  transcripts/*.jsonl[.gz]       first line {"meta": true, "goal": ...}; later lines are turns

Our items are recognised by the "[ID]" tag that build_goal() puts at the start of every goal.
Every reader is tolerant: a missing file, a half-written file or an unknown field only means
"no information from this source".
"""
from __future__ import annotations

import gzip
import json
import os
from dataclasses import dataclass
from pathlib import Path

QUEUED, RUNNING, DONE, FAILED, UNSEEN = "queued", "running", "done", "failed", "unseen"

#: worker.status values that mean the worker has stopped.
_STOPPED_STATUS = {"done", "failed", "error", "stuck", "aborted", "interrupted", "closed", "cancelled"}
_LIVE_STATUS = {"ready", "waiting", "refuting", "running", "working", "starting"}
_OK_OUTCOMES = {"DONE", "OK", "SUCCESS"}

#: A transcript that has not grown for this long, with an answer in it, counts as finished
#: when no other source says anything (the status snapshot is replaced by the next run).
TRANSCRIPT_IDLE_DONE_S = 900
_TAIL_BYTES = 2_000_000


@dataclass
class Observation:
    state: str = UNSEEN
    outcome: str = ""
    answer: str = ""
    reason: str = ""
    worker: str = ""
    task_id: str = ""
    started_ts: float | None = None
    finished_ts: float | None = None
    turns: int | None = None
    source: str = ""
    transcript: str = ""


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _read_json(path):
    try:
        return json.loads(_decode(Path(path).read_bytes()))
    except Exception:
        return None


def _num(v):
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _open_lines(path):
    p = str(path)
    if p.endswith(".gz"):
        with gzip.open(p, "rb") as fh:
            return fh.read(_TAIL_BYTES * 4).splitlines()
    size = os.path.getsize(p)
    with open(p, "rb") as fh:
        if size > _TAIL_BYTES:
            fh.seek(size - _TAIL_BYTES)
            fh.readline()  # drop the partial line
        return fh.read().splitlines()


def read_transcript(path) -> dict:
    """-> {goal, answer, last_ts, turns}; empty strings when unreadable."""
    out = {"goal": "", "answer": "", "last_ts": None, "turns": None}
    try:
        lines = _open_lines(path)
    except Exception:
        return out
    try:
        out["last_ts"] = os.path.getmtime(path)
    except OSError:
        pass
    for raw in lines:
        raw = raw.strip()
        if not raw.startswith(b"{"):
            continue
        try:
            obj = json.loads(_decode(raw))
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("meta") and not out["goal"]:
            out["goal"] = str(obj.get("goal") or "")
        elif obj.get("role") == "assistant" and obj.get("text"):
            out["answer"] = str(obj["text"])
            out["turns"] = obj.get("turn", out["turns"])
        t = obj.get("ts")
        if isinstance(t, (int, float)):
            out["last_ts"] = float(t) if out["last_ts"] is None else max(out["last_ts"], float(t))
    return out


def _first_line_goal(path) -> str:
    """The meta goal of a transcript without reading the whole file."""
    try:
        p = str(path)
        if p.endswith(".gz"):
            with gzip.open(p, "rb") as fh:
                first = fh.readline(200_000)
        else:
            with open(p, "rb") as fh:
                first = fh.readline(200_000)
        obj = json.loads(_decode(first))
        if isinstance(obj, dict) and obj.get("meta"):
            return str(obj.get("goal") or "")
    except Exception:
        pass
    return ""


class FleetReader:
    def __init__(self, fleet_dir):
        self.dir = Path(fleet_dir)
        self._meta_cache: dict = {}

    # ---- status.json
    def _status(self) -> dict:
        d = _read_json(self.dir / "status.json")
        return d if isinstance(d, dict) else {}

    def fleet_summary(self) -> dict:
        st = self._status()
        keys = ("running", "paused", "max_concurrent", "disk_floor_gb", "free_disk_gb",
                "total", "done_count", "queued", "run_label")
        return {k: st.get(k) for k in keys if k in st}

    def fleet_running(self) -> bool:
        return bool(self._status().get("running"))

    def _group_merged(self, st: dict, worker: dict):
        """True/False when the fan-out group is known, None when it is not."""
        fan = worker.get("fanout") if isinstance(worker.get("fanout"), dict) else {}
        cid = worker.get("campaign_id") or fan.get("campaign_id")
        if not cid:
            return None
        for g in st.get("groups") or []:
            if isinstance(g, dict) and g.get("group_id") == cid:
                return str(g.get("merge_state") or "") == "merged"
        return None

    def _from_worker(self, w: dict, st: dict) -> Observation:
        status = str(w.get("status") or "").lower()
        outcome = str(w.get("outcome") or "").upper()
        events = [e for e in (w.get("phase_events") or []) if isinstance(e, dict)]
        ts = [_num(e.get("ts")) for e in events if _num(e.get("ts"))]
        started = None
        for e in events:
            if str(e.get("event")) in ("ready", "waiting", "running") and _num(e.get("ts")):
                started = _num(e.get("ts"))
                break
        ob = Observation(source="status.json", worker=str(w.get("name") or ""),
                         task_id=str(w.get("task_id") or ""), outcome=outcome,
                         reason=str(w.get("reason") or ""), answer=str(w.get("last") or ""),
                         transcript=str(w.get("transcript") or ""), started_ts=started)
        stopped = status in _STOPPED_STATUS or (outcome in _OK_OUTCOMES) or \
            (bool(outcome) and outcome != "FANOUT" and status not in _LIVE_STATUS)
        if outcome == "FANOUT":
            merged = self._group_merged(st, w)
            if merged is False:
                stopped = False
            elif merged is True:
                ob.outcome = "DONE"
                stopped = True
        if stopped:
            ob.finished_ts = max(ts) if ts else None
            ob.state = DONE if ob.outcome in _OK_OUTCOMES or (not ob.outcome and status == "done") \
                else FAILED
            if not ob.outcome and ob.state == DONE:
                ob.outcome = "DONE"
        elif status in _LIVE_STATUS or started:
            ob.state = RUNNING
        else:
            ob.state = QUEUED
        return ob

    def _task_outcome(self, ob: Observation) -> None:
        if not ob.task_id:
            return
        d = _read_json(self.dir / "tasks" / "done" / (ob.task_id + ".outcome.json"))
        if not isinstance(d, dict):
            return
        res = d.get("result") if isinstance(d.get("result"), dict) else {}
        if res.get("outcome") and not ob.outcome:
            ob.outcome = str(res["outcome"]).upper()
        if res.get("turns") is not None and ob.turns is None:
            ob.turns = res.get("turns")
        if res.get("reason") and not ob.reason:
            ob.reason = str(res["reason"])
        if res.get("worker") and not ob.worker:
            ob.worker = str(res["worker"])
        done_ts = _num(d.get("ts_done"))
        if done_ts:
            ob.finished_ts = done_ts

    # ---- transcripts
    def _transcript_paths(self, since_ts: float):
        tdir = self.dir / "transcripts"
        try:
            names = os.listdir(tdir)
        except OSError:
            return
        for n in names:
            if not (n.endswith(".jsonl") or n.endswith(".jsonl.gz")):
                continue
            p = tdir / n
            try:
                if os.path.getmtime(p) >= since_ts - 60:
                    yield p
            except OSError:
                continue

    def _scan_transcripts(self, tags: dict, since_ts: float) -> dict:
        """tag -> newest transcript path whose goal carries it."""
        found: dict = {}
        for p in self._transcript_paths(since_ts):
            key = str(p)
            goal = self._meta_cache.get(key)
            if goal is None:
                goal = _first_line_goal(p)
                if goal:
                    self._meta_cache[key] = goal
            for iid, tag in tags.items():
                if goal and tag in goal:
                    prev = found.get(iid)
                    if prev is None or os.path.getmtime(p) > os.path.getmtime(prev):
                        found[iid] = p
        return found

    # ---- public
    def observe(self, items: dict, since_ts: float, now_ts: float) -> dict:
        """items: id -> goal tag, e.g. {"A1": "[A1]"}. Returns id -> Observation."""
        st = self._status()
        workers = [w for w in (st.get("workers") or []) if isinstance(w, dict)]
        result: dict = {}
        missing = {}
        for iid, tag in items.items():
            cands = [w for w in workers
                     if tag in str(w.get("goal") or "") or tag in str(w.get("goal_summary") or "")]
            if cands:
                def rank(w):
                    evs = [_num(e.get("ts")) or 0 for e in (w.get("phase_events") or [])
                           if isinstance(e, dict)]
                    return (max(evs) if evs else 0)
                best = max(cands, key=rank)
                ob = self._from_worker(best, st)
                self._task_outcome(ob)
                result[iid] = ob
            else:
                missing[iid] = tag
        tr = self._scan_transcripts(items, since_ts) if items else {}
        for iid in items:
            ob = result.get(iid)
            path = tr.get(iid) or (ob.transcript if ob and ob.transcript else "")
            info = read_transcript(path) if path else None
            if ob is None:
                if not info or not info["goal"]:
                    result[iid] = Observation()
                    continue
                ob = Observation(source="transcript", transcript=str(path), turns=info["turns"])
                idle = now_ts - (info["last_ts"] or now_ts)
                if info["answer"] and idle >= TRANSCRIPT_IDLE_DONE_S:
                    ob.state, ob.outcome, ob.finished_ts = DONE, "INFERRED", info["last_ts"]
                else:
                    ob.state = RUNNING
                result[iid] = ob
            if info and info["answer"]:
                # The transcript holds the full final message; status.json's `last` may be cut.
                if len(info["answer"]) >= len(ob.answer):
                    ob.answer = info["answer"]
                if ob.turns is None:
                    ob.turns = info["turns"]
            if ob.transcript == "" and path:
                ob.transcript = str(path)
        return result

    def job_waiting(self, job_id: str) -> bool:
        """Queue route: is this job still in the fleet's queue directories (not yet running)?"""
        for sub in ("pending", "for_fleet", "awaiting", "awaiting_ack", "running"):
            d = self.dir / "tasks" / sub
            try:
                if any(n.startswith(job_id) for n in os.listdir(d)):
                    return True
            except OSError:
                continue
        return False
