"""Fairness bookkeeping: a ledger of question hashes so edits made after the test split was
looked at are detected, and questions promoted from dev to test are flagged."""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Iterable

from .questions import Question

LEDGER_VERSION = 1


def empty_ledger() -> dict:
    return {"version": LEDGER_VERSION, "questions": {}, "dev_ids": [], "test_viewed": {},
            "test_viewed_at": None, "late_test_ids": []}


def load_ledger(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_file():
        return empty_ledger()
    led = json.loads(p.read_text(encoding="utf-8"))
    base = empty_ledger()
    base.update(led)
    return base


def save_ledger(path: str | Path, ledger: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(ledger, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                          encoding="utf-8")


def check_changes(questions: Iterable[Question], ledger: dict) -> list[str]:
    """Warnings about test-set integrity, computed against the ledger (no side effects)."""
    qs = {q.id: q for q in questions}
    warns: list[str] = []
    viewed: dict = ledger.get("test_viewed") or {}
    when = ledger.get("test_viewed_at") or "an earlier run"
    for qid, old_hash in sorted(viewed.items()):
        q = qs.get(qid)
        if q is None:
            warns.append("test question %s was removed after the test split was viewed (%s)" % (qid, when))
        elif q.content_hash() != old_hash:
            warns.append("test question %s was edited after the test split was viewed (%s); "
                         "its score is no longer comparable with earlier test results" % (qid, when))
        elif q.split != "test":
            warns.append("question %s was test when viewed and is now %s" % (qid, q.split))
    for qid in sorted(ledger.get("late_test_ids") or []):
        if qid in qs:
            warns.append("test question %s was added after the test split was viewed" % qid)
    dev_ids = set(ledger.get("dev_ids") or [])
    for q in qs.values():
        if q.split == "test" and q.id in dev_ids:
            warns.append("question %s was used as dev (tuning) and is now in test; it is burned for "
                         "final evaluation" % q.id)
    if viewed:
        for q in qs.values():
            if q.split == "test" and q.id not in viewed and q.id not in (ledger.get("late_test_ids") or []):
                warns.append("test question %s was added after the test split was viewed" % q.id)
    return warns


def record_run(questions: Iterable[Question], ledger: dict, test_scored: bool,
               now: str | None = None) -> dict:
    """Update the ledger after a run. Baselines for test questions are kept from their FIRST view."""
    now = now or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    qs = list(questions)
    snap = ledger.setdefault("questions", {})
    dev_ids = set(ledger.get("dev_ids") or [])
    for q in qs:
        snap[q.id] = {"hash": q.content_hash(), "split": q.split}
        if q.split == "dev":
            dev_ids.add(q.id)
    ledger["dev_ids"] = sorted(dev_ids)
    if test_scored:
        viewed = ledger.setdefault("test_viewed", {})
        had_view = bool(viewed)
        late = set(ledger.get("late_test_ids") or [])
        for q in qs:
            if q.split == "test" and q.id not in viewed:
                viewed[q.id] = q.content_hash()
                if had_view:
                    late.add(q.id)
        ledger["late_test_ids"] = sorted(late)
        if not had_view or ledger.get("test_viewed_at") is None:
            ledger["test_viewed_at"] = now
    return ledger


def rebaseline(questions: Iterable[Question], ledger: dict) -> dict:
    """Accept the current test questions as the new baseline (an explicit, auditable act)."""
    ledger["test_viewed"] = {q.id: q.content_hash() for q in questions if q.split == "test"}
    ledger["late_test_ids"] = []
    ledger.setdefault("rebaselined", []).append(
        _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return ledger
