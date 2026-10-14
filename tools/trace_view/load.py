# -*- coding: utf-8 -*-
"""Read the server's existing JSONL records for a time window, tolerantly and as a stream.

Nothing here writes. The inputs are the append-only files in the state directory:

  tool_events.jsonl          call / outcome pairs written by tools/tool_ledger.py
  mcp_inbound.jsonl          JSON-RPC arrivals written by tools/obs_log.py
  fleet_submit_refusals.jsonl  refused fleet_submit calls
  lock_refusals.jsonl        lock refusals and grants
  send_stage.jsonl           bridge send stages (optional)

A ledger can be tens of megabytes, so the loader never reads a file whole: it bisects on the
byte offset to the window's start (the files are written in time order), then streams lines,
skipping by a cheap timestamp regex before it pays for json.loads. A file that is missing, a
line that is not JSON, or a row of the wrong shape is counted in `health` and otherwise ignored.
"""
from __future__ import annotations

import json
import os
import re

TOOL_EVENTS = "tool_events.jsonl"
INBOUND = "mcp_inbound.jsonl"
SUBMIT_REFUSALS = "fleet_submit_refusals.jsonl"
LOCK_REFUSALS = "lock_refusals.jsonl"
SEND_STAGE = "send_stage.jsonl"

#: An outcome is written when the tool returns, which can be long after its call; read this far
#: past the end of the window so a call near the end still finds its outcome.
OUTCOME_GRACE_S = 3600.0
#: Rows are appended by several processes, so file order is time order only approximately.
ORDER_MARGIN_S = 3600.0

_TS_RE = re.compile(rb'"ts":\s*([0-9]+(?:\.[0-9]+)?)')
_HEAD_BYTES = 600


def default_state_dir() -> str:
    """Where the server writes its state: explicit env, then the tool ledger's own directory."""
    env = os.environ.get("TRACE_VIEW_STATE_DIR")
    if env:
        return env
    try:
        from tools import tool_ledger
        return os.path.dirname(str(tool_ledger._repo_path()))
    except Exception:
        repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return os.path.join(repo, ".fleet")


def _line_ts(raw: bytes):
    m = _TS_RE.search(raw[:_HEAD_BYTES])
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _ts_after(fh, offset: int, size: int):
    """(timestamp, line_start) of the first complete line that starts at or after `offset`."""
    if offset > 0:
        fh.seek(offset - 1)
        fh.readline()          # finish the line we landed inside
    else:
        fh.seek(0)
    pos = fh.tell()
    for _ in range(50):
        if pos >= size:
            return None, size
        raw = fh.readline()
        if not raw:
            return None, size
        ts = _line_ts(raw)
        if ts is not None:
            return ts, pos
        pos = fh.tell()
    return None, pos


def _seek_start(fh, size: int, target: float) -> int:
    """Byte offset from which a forward scan will see every row with ts >= target."""
    lo, hi = 0, size
    best = 0
    while hi - lo > 65536:
        mid = (lo + hi) // 2
        ts, pos = _ts_after(fh, mid, size)
        if ts is None:
            hi = mid
        elif ts < target:
            lo = mid
            best = pos
        else:
            hi = mid
    return best


def iter_rows(path: str, since, until, health: dict, name: str, grace: float = 0.0):
    """Yield JSON objects with since <= ts <= until + grace. Never raises."""
    info = health.setdefault(name, {"present": False, "rows": 0, "bad": 0})
    try:
        size = os.path.getsize(path)
        fh = open(path, "rb")
    except OSError:
        return
    info["present"] = True
    upper = None if until is None else until + grace
    stop_after = None if upper is None else upper + ORDER_MARGIN_S
    try:
        with fh:
            if since is not None and size > 262144:
                fh.seek(_seek_start(fh, size, since - ORDER_MARGIN_S))
                # land on a line boundary
                if fh.tell() > 0:
                    fh.seek(fh.tell() - 1)
                    fh.readline()
            else:
                fh.seek(0)
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                ts = _line_ts(raw)
                if ts is not None:
                    if stop_after is not None and ts > stop_after:
                        break
                    if since is not None and ts < since:
                        continue
                    if upper is not None and ts > upper:
                        continue
                try:
                    row = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    info["bad"] += 1
                    continue
                if not isinstance(row, dict):
                    info["bad"] += 1
                    continue
                rts = row.get("ts")
                if not isinstance(rts, (int, float)):
                    info["no_ts"] = info.get("no_ts", 0) + 1     # older rows carry no time
                    continue
                if since is not None and rts < since:
                    continue
                if upper is not None and rts > upper:
                    continue
                info["rows"] += 1
                yield row
    except Exception:
        info["bad"] += 1
        return


def _paths(state_dir: str, name: str):
    """The live file and, when it exists, the one rotation generation beside it."""
    base = os.path.join(state_dir, name)
    out = [base + ".1", base]
    return [p for p in out if os.path.exists(p)] or [base]


def load(state_dir=None, since=None, until=None, session=None) -> dict:
    """Everything the viewer needs, as plain dicts. `session` is a prefix filter.

    Returns {"calls": {id: row}, "outcomes": {id: row}, "inbound": [...], "lock": [...],
    "submit_refusals": [...], "send_stage": [...], "health": {...}, "state_dir": str}.
    """
    state_dir = state_dir or default_state_dir()
    health: dict = {}
    calls, outcomes = {}, {}
    for path in _paths(state_dir, TOOL_EVENTS):
        for row in iter_rows(path, since, until, health, TOOL_EVENTS, grace=OUTCOME_GRACE_S):
            rid = row.get("id")
            if not isinstance(rid, str):
                continue
            ev = row.get("event")
            if ev == "call":
                if until is not None and row["ts"] > until:
                    continue
                calls[rid] = row
            elif ev == "outcome":
                outcomes[rid] = row
    inbound, lock, submit, stage = [], [], [], []
    for path in _paths(state_dir, INBOUND):
        inbound.extend(iter_rows(path, since, until, health, INBOUND))
    for path in _paths(state_dir, LOCK_REFUSALS):
        for row in iter_rows(path, since, until, health, LOCK_REFUSALS):
            if row.get("event") in ("refused", "granted"):
                lock.append(row)
    for path in _paths(state_dir, SUBMIT_REFUSALS):
        submit.extend(iter_rows(path, since, until, health, SUBMIT_REFUSALS))
    for path in _paths(state_dir, SEND_STAGE):
        stage.extend(iter_rows(path, since, until, health, SEND_STAGE))
    for lst in (inbound, lock, submit, stage):
        lst.sort(key=lambda r: r.get("ts", 0))
    if session:
        pre = str(session)
        calls = {k: v for k, v in calls.items() if str(v.get("session") or "").startswith(pre)}
        outcomes = {k: v for k, v in outcomes.items() if k in calls}
        inbound = _inbound_for(inbound, pre)
        lock = [r for r in lock if str(r.get("session") or "").startswith(pre)]
    return {"calls": calls, "outcomes": outcomes, "inbound": inbound, "lock": lock,
            "submit_refusals": submit, "send_stage": stage, "health": health,
            "state_dir": state_dir}


def attribute_inbound(inbound: list) -> list:
    """Give an `initialize` row (which arrives before a session id exists) the session of the
    next row from the same client within a few seconds. Returns new dicts; input is unchanged."""
    out = [dict(r) for r in inbound]
    for i, row in enumerate(out):
        if row.get("session"):
            continue
        for later in out[i + 1:i + 40]:
            if later.get("ts", 0) - row.get("ts", 0) > 5.0:
                break
            if (later.get("session") and later.get("client_ip") == row.get("client_ip")
                    and later.get("ua") == row.get("ua")):
                row["session"] = later["session"]
                row["attributed"] = True
                break
    return out


def _inbound_for(inbound: list, prefix: str) -> list:
    return [r for r in attribute_inbound(inbound) if str(r.get("session") or "").startswith(prefix)]
