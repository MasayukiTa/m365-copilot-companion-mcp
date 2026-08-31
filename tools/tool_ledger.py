# -*- coding: utf-8 -*-
"""What a tool was asked to do and what came back, written down where it can be cited later.

THE GAP THIS FILLS, MEASURED. The session store holds 122 MB and 15,605 turns, and NONE of it
records a tool call. 26.8 MB of user text, 4.8 MB of assistant text, and nothing else: no tool
name, no arguments, no result, no error. Everything this system knows about its own work is the
prose the two sides wrote about it.

WHAT THAT COSTS, also measured. Workers claim DONE with precision 0.718 -- 11 of 39 claims wrong
on a 40-instance slice. The refuter that exists to catch those reads the worker's ACCOUNT of
what it did, because there is nothing else to read, so it judges hearsay. An experiment on
whether skills were being consulted had to add its own side-channel for the same reason: the
transcript could not say whether a tool had been called. Every question of the form "did it
actually do that" is currently unanswerable.

ASSISTANT PROSE IS NOT EVIDENCE. That is the whole design rule. A claim and the record of the
act have to come from different places, or a worker that is mistaken -- or lying -- writes both.

APPEND-ONLY JSONL, ON PURPOSE. Two records per call, linked by one id: the CALL, written before
the tool runs, and the OUTCOME, written after. Not one record written at the end, because a call
that never returns is exactly the case worth seeing: a crash, a timeout, a killed process leave
a call with no outcome, and that orphan is a finding rather than a gap. No database, no UI, no
replay engine -- those can be built on this, and cannot be recovered without it.

WHAT IS DELIBERATELY NOT STORED: chain of thought (there is none to store here, and it would be
the wrong thing to keep), and unbounded results. A result is truncated and hashed, so the record
stays small and a later claim about what came back can still be checked against it.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER_PATH = os.path.join(_REPO, ".fleet", "tool_events.jsonl")

SCHEMA_VERSION = 1

#: How much of an argument blob or a result is kept inline. Enough to recognise what happened,
#: bounded so a ledger cannot become the thing that fills the disk -- which on this machine is
#: the binding constraint, and has already stopped a benchmark run once.
MAX_INLINE = 2000

#: Argument names whose VALUE never goes in, whatever tool they belong to. The ledger is a file
#: on disk that outlives the session; a password written once is written forever.
SECRET_ARGS = {"password", "passwd", "secret", "token", "unlock_token", "api_key", "apikey",
               "authorization", "auth", "credential", "credentials", "private_key"}

_LOCK = threading.Lock()


def _repo_path():
    """Resolved at call time so a test (and the repo-wide isolation fixture) can move it."""
    return LEDGER_PATH


def new_call_id() -> str:
    return uuid.uuid4().hex[:16]


def _digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:16]


def _bounded(value):
    """A value small enough to store, with the full thing still identifiable by its digest."""
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False,
                                                               default=str)
    except Exception:
        text = str(value)
    full = len(text)
    return {"text": text[:MAX_INLINE], "len": full, "sha16": _digest(text),
            "truncated": full > MAX_INLINE}


def redact_args(arguments) -> dict:
    """Arguments with secret VALUES removed and the rest bounded.

    The NAMES stay. "there was a password argument" is evidence; the password is not.
    """
    if not isinstance(arguments, dict):
        return {"_": _bounded(arguments)}
    out = {}
    for key, value in arguments.items():
        if str(key).strip().lower() in SECRET_ARGS:
            out[key] = {"redacted": True, "sha16": _digest(str(value))}
        else:
            out[key] = _bounded(value)
    return out


def _append(row: dict) -> None:
    """Best effort, never raises. A ledger that can fail a tool call is worse than no ledger."""
    try:
        path = _repo_path()
        with _LOCK:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def record_call(tool: str, arguments=None, *, task: str = "", worker: str = "",
                turn=None, call_id: str = "", ts: float = None) -> str:
    """Write the CALL record, BEFORE the tool runs. Returns the id to pass to record_outcome.

    Before, not after, so a call that never returns still leaves a trace. A ledger written only
    on success records the runs that did not need recording.
    """
    cid = call_id or new_call_id()
    _append({
        "schema": SCHEMA_VERSION,
        "event": "call",
        "id": cid,
        "ts": float(ts if ts is not None else time.time()),
        "tool": str(tool or "")[:120],
        "task": str(task or "")[:120],
        "worker": str(worker or "")[:64],
        "turn": turn,
        "args": redact_args(arguments),
    })
    return cid


def record_outcome(call_id: str, *, ok: bool, result=None, error: str = "",
                   ts: float = None, duration_s: float = None) -> None:
    """Write the OUTCOME record for a call. Linked by id, never merged into the call record."""
    _append({
        "schema": SCHEMA_VERSION,
        "event": "outcome",
        "id": str(call_id or ""),
        "ts": float(ts if ts is not None else time.time()),
        "ok": bool(ok),
        "duration_s": (round(float(duration_s), 3) if duration_s is not None else None),
        "error": str(error or "")[:MAX_INLINE],
        "result": _bounded(result) if result is not None else None,
    })


def read(path: str = None):
    """Every record, in order. Malformed lines are skipped rather than raising -- a ledger that
    cannot be read because one line is torn is a ledger that stops being consulted."""
    rows = []
    try:
        with open(path or _repo_path(), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows


def orphans(rows=None):
    """Calls with no outcome. THE POINT OF WRITING TWO RECORDS.

    A crash, a timeout, or a killed process leaves one of these. It is a finding -- the tool was
    entered and never came back -- and it is invisible to any scheme that writes a single record
    when a call completes.
    """
    rows = read() if rows is None else rows
    seen_outcome = {r.get("id") for r in rows if r.get("event") == "outcome"}
    return [r for r in rows
            if r.get("event") == "call" and r.get("id") not in seen_outcome]


def for_task(task: str, rows=None):
    """Every call made under one task, with its outcome attached where there is one.

    This is what a verifier reads instead of the worker's account of itself.
    """
    rows = read() if rows is None else rows
    outcomes = {r.get("id"): r for r in rows if r.get("event") == "outcome"}
    out = []
    for r in rows:
        if r.get("event") != "call" or (task and r.get("task") != task):
            continue
        out.append({"call": r, "outcome": outcomes.get(r.get("id"))})
    return out
