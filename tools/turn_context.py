# -*- coding: utf-8 -*-
"""Which worker's turn was in flight when a tool call arrived? Answered from the coordinator.

WHY THIS EXISTS. tools/tool_ledger.py attributed a call to a task/worker only when the worker's
OWN MCP session called claim_turn / heartbeat / read_job_context with a job_id. Real Copilot fleet
workers never do (measured over two days: 1 claim_turn in 5,718 calls, attribution fill 0.0% of
37,156 rows), and the prompt they are given never asks them to. A fix that depends on the
worker volunteering its identity is a fix that depends on the thing that already failed.

The COORDINATOR (relay/relay_fleet.py) already knows, without asking anybody, which worker it
sent a turn to, for which job, and when the turn went out and came back. This module is the
hand-off: the coordinator appends those facts to `.fleet/turn_context.jsonl`, and the ledger
writer (a different process, the MCP server) reads them back to label a call it could not label.

FILE FORMAT. Append-only JSON lines, two rows per turn:
  {"event":"open",  "worker","job","run","turn","t_send","ts","mono","proc","pid"}
  {"event":"close", "worker","job","run","turn","t_send","t_done","ts","mono","proc","pid"}
`ts` is the wall clock (epoch seconds) when the row was written; `mono` is time.monotonic() of
the writing process. A turn that never closes (worker died, coordinator crashed) stays open and
is treated as abandoned MAX_OPEN_S after it started.

CLOCK BASIS AND TOLERANCE. MATCHING USES WALL-CLOCK EPOCH TIME (time.time()). The coordinator
and the MCP server are two processes, and time.monotonic() has a per-process meaning in general
(it is stored for the reader's benefit, and the ledger's `proc` field says when it is
comparable), so the only basis both processes share is the system clock of this one machine.
Both run on the same host, so there is no cross-host skew to budget for; SLACK_S = 1.5 s is
added at BOTH edges of a window to absorb (a) the gap between the send and the model's first tool
call being stamped on the other side, (b) a call that lands just after the reply was read, and
(c) coarse Windows clock granularity. Slack makes windows of consecutive turns of different
workers touch, and touching windows are AMBIGUOUS, not guessed.

THE OVERLAP RULE (never guess):
  * exactly one worker has a matching window  -> attribute it ("window")
  * several workers match, and this MCP session was earlier bound by an unambiguous match to one
    of them -> reuse that binding ("session-window")
  * several match and there is no usable binding -> leave task/worker EMPTY ("ambiguous")
  * none match -> leave empty, no label.
relay/turn_windows.py measured the real limit of this: with a large fleet most moments have
several workers in flight, so most calls will stay ambiguous. That is the honest result, and the
report (scripts/tool_event_report.py) shows its share.

BOUNDED. The writer rotates the file to `.1` at WRITE_MAX_BYTES, and relay/fleet_retention.py's
cap_jsonl also sweeps every `.fleet/*.jsonl`, so this file is capped like tool_events.jsonl. The
reader only parses the last READ_TAIL_BYTES, which is far more than MAX_OPEN_S of turns.

A failure to write must never break a turn, and a failure to read must never break a tool call:
every public function swallows its own errors.
"""
from __future__ import annotations

import json
import os
import threading
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTEXT_PATH = os.path.join(_REPO, ".fleet", "turn_context.jsonl")

#: Window edge tolerance, seconds, applied to both ends. See CLOCK BASIS AND TOLERANCE above.
SLACK_S = 1.5
#: A window with no close row claims events for at most this long after it started.
MAX_OPEN_S = 1800.0
#: The writer's own ceiling (the retention sweep's 64 MB is a backstop, not a budget).
WRITE_MAX_BYTES = 8 * 1024 * 1024
#: How much of the file's end the reader parses. ~250 bytes a row: ~4000 rows, i.e. well over
#: MAX_OPEN_S of turns for a 30-worker fleet.
READ_TAIL_BYTES = 1024 * 1024

_PROC = None
_LOCK = threading.Lock()
_CACHE = {"key": None, "path": None, "windows": []}


def _proc():
    global _PROC
    if _PROC is None:
        try:
            from tools import tool_ledger
            _PROC = tool_ledger._PROC
        except Exception:
            _PROC = "p%d" % os.getpid()
    return _PROC


def _rotate_if_large(path):
    try:
        if os.path.getsize(path) < WRITE_MAX_BYTES:
            return
        os.replace(path, path + ".1")
    except OSError:
        pass


def _append(row):
    try:
        path = CONTEXT_PATH
        row.update({"ts": round(time.time(), 3), "mono": round(time.monotonic(), 4),
                    "proc": _proc(), "pid": os.getpid()})
        line = json.dumps(row, ensure_ascii=False)
        with _LOCK:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _rotate_if_large(path)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


#: Optional fan-out identity carried on a row, additive: old rows lack these and old readers
#: ignore them. Campaign task ids ('<cid>-<i>') live in a different id space from the job id,
#: so the join needs them written down at the source rather than translated later.
IDENT_KEYS = ("campaign_id", "task_id", "parent_task_id", "root_id", "role")


def fanout_identity(envelope):
    """{campaign_id, task_id, parent_task_id, root_id, role} of a task envelope, empty values
    omitted. {} for a worker that is not a fan-out child. Never raises."""
    try:
        if envelope is None:
            return {}
        meta = getattr(envelope, "metadata", None) or {}
        raw = {"campaign_id": getattr(envelope, "campaign_id", ""),
               "task_id": getattr(envelope, "task_id", ""),
               "parent_task_id": getattr(envelope, "parent_task_id", ""),
               "root_id": meta.get("root_id", "") if isinstance(meta, dict) else "",
               "role": getattr(envelope, "role", "")}
        return {k: str(v)[:120] for k, v in raw.items() if v}
    except Exception:
        return {}


def _ident(ident):
    if not isinstance(ident, dict):
        return {}
    return {k: str(ident[k])[:120] for k in IDENT_KEYS if ident.get(k)}


def record_open(worker, job, run, turn, t_send, ident=None):
    """The coordinator sent turn `turn` to `worker` at wall-clock `t_send`. Never raises."""
    if not worker:
        return
    try:
        _append({"event": "open", "worker": str(worker)[:64], "job": str(job or "")[:120],
                 "run": str(run or "")[:120], "turn": turn, "t_send": round(float(t_send), 3),
                 **_ident(ident)})
    except Exception:
        pass


def record_close(worker, job, run, turn, t_send, t_done, ident=None):
    """The reply to that turn came back at wall-clock `t_done`. Never raises."""
    if not worker:
        return
    try:
        _append({"event": "close", "worker": str(worker)[:64], "job": str(job or "")[:120],
                 "run": str(run or "")[:120], "turn": turn,
                 "t_send": round(float(t_send), 3), "t_done": round(float(t_done), 3),
                 **_ident(ident)})
    except Exception:
        pass


def _build(rows):
    """rows (file order) -> list of window dicts {worker, job, run, turn, start, end|None}."""
    last = {}       # worker -> its most recent window
    out = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("worker"):
            continue
        try:
            w = str(r["worker"])
            start = float(r["t_send"])
            if r.get("event") == "open":
                prev = last.get(w)
                if prev is not None and prev["end"] is None:
                    prev["end"] = start     # a new send supersedes a turn that never closed
                win = {"worker": w, "job": str(r.get("job") or ""), "run": str(r.get("run") or ""),
                       "turn": r.get("turn"), "start": start, "end": None,
                       "ident": _ident(r)}
                last[w] = win
                out.append(win)
            elif r.get("event") == "close":
                end = float(r["t_done"])
                prev = last.get(w)
                if prev is not None and prev["turn"] == r.get("turn") and abs(prev["start"] - start) < 0.01:
                    prev["end"] = end
                else:   # its open row fell off the tail (or was never written)
                    win = {"worker": w, "job": str(r.get("job") or ""),
                           "run": str(r.get("run") or ""), "turn": r.get("turn"),
                           "start": start, "end": end, "ident": _ident(r)}
                    last[w] = win
                    out.append(win)
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _windows():
    """Parsed windows, cached by the file's (mtime, size). [] when missing or unreadable."""
    path = CONTEXT_PATH
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = (st.st_mtime_ns, st.st_size)
    with _LOCK:
        if _CACHE["key"] == key and _CACHE["path"] == path:
            return _CACHE["windows"]
    rows = []
    try:
        with open(path, "rb") as fh:
            if st.st_size > READ_TAIL_BYTES:
                fh.seek(st.st_size - READ_TAIL_BYTES)
                fh.readline()       # land on a line boundary
            for raw in fh:
                try:
                    rows.append(json.loads(raw.decode("utf-8", "replace")))
                except ValueError:
                    continue        # a corrupt line costs that line, not the file
    except OSError:
        return []
    windows = _build(rows)
    with _LOCK:
        _CACHE.update(key=key, path=path, windows=windows)
    return windows


def candidates(ts):
    """[(worker, task)] for every distinct worker whose turn window contains wall-clock `ts`.

    `task` is the admitted-goal id when the coordinator had one, else the run id.
    """
    found = {}
    try:
        for win in _windows():
            hi = (win["end"] if win["end"] is not None else win["start"] + MAX_OPEN_S) + SLACK_S
            if win["start"] - SLACK_S <= ts <= hi:
                found[win["worker"]] = win["job"] or win["run"]
    except Exception:
        return []
    return sorted(found.items())


def identity_of(worker, ts):
    """Fan-out identity dict of `worker`'s window containing `ts`, or {}.

    {} when no such window carries one, and also when the worker's matching windows (touching
    turns share the slack) disagree: an identity is never picked between two. Never raises.
    """
    try:
        found = []
        for win in _windows():
            if win["worker"] != worker:
                continue
            hi = (win["end"] if win["end"] is not None else win["start"] + MAX_OPEN_S) + SLACK_S
            if win["start"] - SLACK_S <= ts <= hi:
                found.append(win.get("ident") or {})
        if found and all(f == found[0] for f in found):
            return dict(found[0])
    except Exception:
        pass
    return {}
