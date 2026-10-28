# -*- coding: utf-8 -*-
"""What the gateway is running RIGHT NOW, so an operator can see the jam without a restart.

WHY THIS EXISTS. 2026-10-27..28 the tool gateway jammed three times. Each time
initialize / tools/list still answered in a few seconds, so the server looked alive, yet no
tools/call started -- a single unbounded find_files / glob was holding the worker threads and
everything behind it waited. The only fact available was "tools/list works but tools/call does
not", which does not say WHICH call is holding the floor or for HOW LONG, so the only recovery
was to restart the server and lose that evidence (issue #328, proposal 2).

This module is that missing fact: a process-local, thread-safe inventory of the calls that have
entered the gateway's execute path and not yet left it. The call_tool gateway calls begin()
just before it runs a tool and end() in its finally, so a call that never returns stays visible
(the same reasoning tool_ledger.record_call uses for writing BEFORE the tool runs). main.py
exposes a snapshot over an HTTP route that is SEPARATE from tools/list and from the tool path
itself, so reading "what is running" never has to queue behind the thing that is stuck.

WHAT IT IS NOT. It does not stop, kill, or cancel anything -- it only reports. Terminating a
stuck call needs a worker-process model this server does not have (proposal 1/5; see
explain.md). Visibility is the part that is safe to add to the current in-thread model, and it
is the part that turns "restart and hope" into "name the occupant".

The elapsed seconds come from time.monotonic(), which never goes backwards, so an elapsed value
is always a real duration even if the wall clock is adjusted mid-call. started_ts is wall-clock
(time.time()), reported so a human reading the snapshot can line it up with the ledger and the
logs; it is for display, never for arithmetic.
"""
from __future__ import annotations

import itertools
import threading
import time

#: One lock guards the dict. Every operation under it is O(number of in-flight calls), which is
#: bounded by the worker-thread count (tens), so holding it is cheap -- the snapshot route can
#: never be the thing that blocks.
_LOCK = threading.Lock()

#: token -> record. token is a monotonic counter, unique within this process, so two calls to
#: the same tool at the same instant still get distinct entries and end() closes exactly the one
#: begin() opened. Keyed on the token, NOT on the tool name, for that reason.
_INFLIGHT: dict = {}

_COUNTER = itertools.count(1)


def begin(tool: str, *, call_id: str = "", started_ts: float = None,
          started_mono: float = None) -> int:
    """Record that `tool` has entered the execute path. Returns a token to pass to end().

    call_id, when given, is the tool_ledger call id for the same call, so a reader can join the
    live snapshot to the persisted ledger row. started_ts / started_mono let the caller reuse
    the timestamps it already took (the gateway's _t0) instead of taking them twice; both
    default to now. Never raises: a bookkeeping failure must not stop a tool from running, so
    the gateway wraps this in try/except, and this keeps its own side simple and total.
    """
    token = next(_COUNTER)
    rec = {
        "token": token,
        "tool": str(tool or "")[:120],
        "call_id": str(call_id or ""),
        "started_ts": float(started_ts if started_ts is not None else time.time()),
        "started_mono": float(started_mono if started_mono is not None else time.monotonic()),
    }
    with _LOCK:
        _INFLIGHT[token] = rec
    return token


def end(token) -> None:
    """Record that the call for `token` has left the execute path. Idempotent and total.

    A token that is not present (already ended, or never begun because begin() raised) is a
    no-op, so end() in a finally can run unconditionally without a guard of its own.
    """
    if token is None:
        return
    with _LOCK:
        _INFLIGHT.pop(token, None)


def executing_count() -> int:
    """How many calls are in the execute path right now."""
    with _LOCK:
        return len(_INFLIGHT)


def snapshot(now_mono: float = None, now_ts: float = None) -> dict:
    """A point-in-time view of every in-flight call, newest-running LAST is not useful here --
    the occupant an operator wants is the OLDEST-running one, so the list is sorted by elapsed
    DESCENDING: the call that has held the floor longest is first.

    Returns a plain dict of built-ins (JSON-serialisable) so an HTTP route can return it
    directly:

        {
          "executing_count": int,
          "executing": [
             {"tool": str, "call_id": str, "started_ts": float, "elapsed_s": float}, ...
          ],
          "longest_elapsed_s": float,   # 0.0 when nothing is running
        }

    elapsed_s is computed from monotonic time so it is always a true, non-negative duration.
    """
    mono = now_mono if now_mono is not None else time.monotonic()
    wall = now_ts if now_ts is not None else time.time()
    with _LOCK:
        rows = list(_INFLIGHT.values())
    out = []
    for r in rows:
        elapsed = mono - r["started_mono"]
        if elapsed < 0:
            elapsed = 0.0
        out.append({
            "tool": r["tool"],
            "call_id": r["call_id"],
            "started_ts": round(r["started_ts"], 3),
            "elapsed_s": round(elapsed, 3),
        })
    out.sort(key=lambda d: d["elapsed_s"], reverse=True)
    longest = out[0]["elapsed_s"] if out else 0.0
    return {
        "executing_count": len(out),
        "executing": out,
        "longest_elapsed_s": longest,
        "as_of_ts": round(wall, 3),
    }


def _reset_for_test() -> None:
    """Drop all in-flight records. For tests only, so one test's leftovers cannot leak into the
    next; production never needs it because end() always runs in the gateway's finally."""
    with _LOCK:
        _INFLIGHT.clear()
