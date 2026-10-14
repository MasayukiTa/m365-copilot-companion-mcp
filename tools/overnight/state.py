"""state.json: one record per item, written atomically so a crash can be resumed."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tools.overnight.common import atomic_write_json

PENDING, SUBMITTING, SUBMITTED, RUNNING = "pending", "submitting", "submitted", "running"
DONE, FAILED, TIMEOUT, SKIPPED = "done", "failed", "timeout", "skipped"
TERMINAL = frozenset({DONE, FAILED, TIMEOUT, SKIPPED})
ACTIVE = frozenset({SUBMITTED, RUNNING})
ALL_STATES = frozenset({PENDING, SUBMITTING, SUBMITTED, RUNNING} | TERMINAL)

_ALLOWED = {
    PENDING: {SUBMITTING, SKIPPED, FAILED},
    SUBMITTING: {SUBMITTED, PENDING, FAILED, SKIPPED},
    SUBMITTED: {RUNNING, DONE, FAILED, TIMEOUT},
    RUNNING: {DONE, FAILED, TIMEOUT},
    DONE: set(), FAILED: set(), TIMEOUT: set(), SKIPPED: set(),
}


def goal_hash(goal: str) -> str:
    return hashlib.sha256(goal.encode("utf-8")).hexdigest()[:16]


class State:
    def __init__(self, path, data=None):
        self.path = Path(path)
        self.data = data or {"version": 1, "config": {}, "items": {}, "order": [], "events": []}

    # ---- persistence
    @classmethod
    def load(cls, path) -> "State":
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        st = cls(p, data)
        st.data.setdefault("config", {})
        st.data.setdefault("items", {})
        st.data.setdefault("order", list(st.data["items"].keys()))
        st.data.setdefault("events", [])
        return st

    def save(self) -> None:
        atomic_write_json(self.path, self.data)

    # ---- items
    @property
    def config(self) -> dict:
        return self.data["config"]

    def ids(self) -> list:
        return list(self.data["order"])

    def get(self, iid: str) -> dict:
        return self.data["items"][iid]

    def add_item(self, iid: str, ask: str, tags=None, max_minutes=None) -> dict:
        if iid in self.data["items"]:
            return self.data["items"][iid]
        rec = {
            "id": iid, "ask": ask, "tags": list(tags or []), "max_minutes": max_minutes,
            "status": PENDING, "goal": "", "goal_hash": "", "variant": 0, "attempts": 0,
            "submitted_ts": None, "started_ts": None, "finished_ts": None,
            "job_id": "", "worker": "", "outcome": "", "answer": "", "answer_source": "",
            "result_dir": "", "error": "", "category": "", "reason": "", "turns": None, "note": "",
        }
        self.data["items"][iid] = rec
        self.data["order"].append(iid)
        return rec

    def set_status(self, iid: str, new: str, **fields) -> None:
        rec = self.get(iid)
        old = rec["status"]
        if new != old and new not in _ALLOWED.get(old, set()):
            raise ValueError("illegal transition %s -> %s for %s" % (old, new, iid))
        rec["status"] = new
        rec.update(fields)
        self.save()

    def update(self, iid: str, **fields) -> None:
        self.get(iid).update(fields)
        self.save()

    def already_submitted(self, iid: str, goal: str) -> bool:
        """True when this id was handed to the fleet with this exact goal (the duplicate guard)."""
        rec = self.data["items"].get(iid)
        if not rec:
            return False
        return rec["status"] in (SUBMITTED, RUNNING, DONE, FAILED, TIMEOUT) and \
            rec["goal_hash"] == goal_hash(goal) and bool(rec["submitted_ts"])

    def by_status(self, *statuses) -> list:
        return [i for i in self.data["order"] if self.data["items"][i]["status"] in statuses]

    def all_terminal(self) -> bool:
        return all(self.data["items"][i]["status"] in TERMINAL for i in self.data["order"])

    def event(self, ts: str, msg: str) -> None:
        ev = self.data["events"]
        ev.append({"ts": ts, "msg": msg})
        del ev[:-200]

    def reset_for_retry(self, iid: str) -> None:
        """Put a failed/timed-out/skipped item back to pending (resume --retry-failed)."""
        rec = self.get(iid)
        if rec["status"] not in (FAILED, TIMEOUT, SKIPPED):
            return
        base = int(rec.get("variant_base", 0)) + int(rec.get("variant", 0)) + 1
        rec.update({"status": PENDING, "submitted_ts": None, "started_ts": None,
                    "finished_ts": None, "outcome": "", "answer": "", "error": "",
                    "category": "", "reason": "", "turns": None, "note": "",
                    "variant_base": base, "variant": 0})
        self.save()
