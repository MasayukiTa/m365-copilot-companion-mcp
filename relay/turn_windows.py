# -*- coding: utf-8 -*-
"""Who could a server-side event at time T have belonged to?

WHY THIS EXISTS. The relay decides "this worker was refused" by pattern-matching the
worker's REPLY TEXT for the server's refusal strings plus a length rule. The server records
every refusal in .fleet/lock_refusals.jsonl with an Mcp-Session-Id, but the relay has no
mapping from a worker to the session the Copilot connector used on its behalf, so it cannot
say which refusal was whose and falls back to reading prose.

WHAT WAS MEASURED, 2026-09-16, before writing any of this. Attributing a session to the
worker whose turn window contains all of that session's calls, over 39 real runs:

    2 workers    100% of sessions uniquely attributable
    3 workers     60%
    8 workers     31%
   15 workers     15%
   27 workers      0%
   96 workers      3%
   -------------------------------------------------------
   overall       470 sessions, 45 unique (10%), 409 ambiguous, 16 orphan

So an exact join is NOT obtainable in general, and anything built as though it were would be
a 10% signal wearing a certainty's clothing. astra put the same conclusion this way: an exact
join for every event requires a reliable identifying signal on every relevant request, or
independently guaranteed isolation, and without independent labels what you have is an
unvalidated heuristic rather than an established error rate.

WHAT THIS DOES INSTEAD. It answers the narrower question honestly: at instant T, WHICH
WORKERS had a turn in flight? If exactly one did, an event at T belongs to it -- that is
exclusivity, not inference, and it needs no prose. If several did, this says so, and the
caller keeps whatever heuristic it had rather than pretending.

The registry is in-process because the relay drives every worker from one process, so the
windows are already known here. Nothing is persisted: this answers about NOW, and a stale
window from a previous run would be worse than no answer.
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional, Tuple

_LOCK = threading.Lock()
#: worker name -> (opened_at, closed_at or None while still in flight)
_WINDOWS: Dict[str, Tuple[float, Optional[float]]] = {}

#: A closed window stays consultable for this long, because a refusal is written at the
#: server a moment before the reply comes back to the relay -- so the event a caller asks
#: about routinely lands just after its own window closed. Long enough to cover that lag,
#: short enough that a worker which finished a minute ago is not still claiming events.
GRACE_S = 20.0


def open_turn(worker: str, ts: Optional[float] = None) -> None:
    """Record that `worker` has a turn in flight from `ts`."""
    if not worker:
        return
    with _LOCK:
        _WINDOWS[str(worker)] = (float(ts if ts is not None else time.time()), None)


def close_turn(worker: str, ts: Optional[float] = None) -> None:
    """Record that `worker`'s turn came back."""
    if not worker:
        return
    with _LOCK:
        cur = _WINDOWS.get(str(worker))
        if cur:
            _WINDOWS[str(worker)] = (cur[0], float(ts if ts is not None else time.time()))


def _contains(window: Tuple[float, Optional[float]], ts: float, now: float) -> bool:
    start, end = window
    if ts < start:
        return False
    if end is None:
        # Still in flight -- but bounded. Measured against the EVENT time rather than against
        # "now": how long after its start a turn may still claim an event is a property of the
        # turn, not of when somebody happens to ask. Bounding against now also made every
        # answer depend on the wall clock, which is untestable at any fixed timestamp.
        return (float(ts) - start) <= MAX_OPEN_S
    return ts <= end + GRACE_S


#: An OPEN window claims everything since it started, which is right while a turn is really
#: in flight and wrong once nothing is driving it. A worker whose process died, a test that
#: called open_turn and never closed it, a coordinator that crashed mid-turn -- each leaves a
#: window that would go on claiming every later event forever, and "exclusively this worker's"
#: is exactly the wrong thing to say about an event nobody was there for. Turns are minutes,
#: not hours; past this an open window is abandoned rather than busy.
MAX_OPEN_S = 1800.0


def candidates(ts: float, now: Optional[float] = None) -> List[str]:
    """Every worker whose turn window contains `ts`. The candidate set, named as such."""
    now = time.time() if now is None else now
    with _LOCK:
        items = list(_WINDOWS.items())
    return sorted(w for w, win in items if _contains(win, float(ts), now))


def exclusive_owner(ts: float, now: Optional[float] = None) -> Optional[str]:
    """The ONLY worker an event at `ts` could belong to, or None when that is not established.

    None covers both "nobody was in flight" and "several were". Neither is an answer, and
    collapsing them into a guess is the move this module exists to refuse.
    """
    c = candidates(ts, now)
    return c[0] if len(c) == 1 else None


def belongs_to(worker: str, ts: float, now: Optional[float] = None) -> bool:
    """Is an event at `ts` EXCLUSIVELY this worker's? False whenever that is not established."""
    return bool(worker) and exclusive_owner(ts, now) == str(worker)


#: NOTHING HERE IS BUILT AHEAD OF A CALLER. `forget(worker)` and `snapshot()` were written
#: with this module and deleted the same day, because CI's unreferenced-symbol guard asked for
#: a caller or a deletion and neither had one. forget() was redundant besides: a window expires
#: on its own through GRACE_S and MAX_OPEN_S, so dropping it explicitly bought nothing.
#: snapshot() was for a log that does not exist yet; if that log is written, it can arrive with
#: its reader.
def reset() -> None:
    """Drop everything. For tests, and for a coordinator starting a fresh run."""
    with _LOCK:
        _WINDOWS.clear()
