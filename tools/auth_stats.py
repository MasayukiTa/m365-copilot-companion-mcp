"""Auth-failure tracker for the MCP server (self-reporting for the health layer).

Incident this closes: Copilot Studio's stored API key desynced from the
server's MCP_API_KEY -> every /mcp tool call started 401ing -> nothing
surfaced this anywhere the operator was looking (the user only saw "local
file access broken", with zero signal that auth was the cause). The
infra principle here is that failures must self-report so the health
layer can auto-remedy -- this module is the smallest piece of state needed
to make a burst of 401s visible without grepping logs.

Design:
  - AuthFailureTracker is a pure, hermetic, thread-safe class: record a
    rejection, prune anything older than the sliding window, and produce a
    small summary dict. No I/O, no imports of fastmcp/starlette/anything
    request-shaped -- fully unit-testable in isolation.
  - A module-level singleton (_TRACKER) is what main.py's middleware and the
    /health route actually touch.
  - write_snapshot() is the ONLY function that does I/O (atomic tmp+replace
    into .fleet/auth_stats.json), and it is best-effort: any failure is
    swallowed so a disk hiccup can never break request handling.

Window default: 10 minutes, matching the "auth_fail_10m" field name used by
/health and the cockpit sidecar file.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

WINDOW_SECONDS_DEFAULT = 600.0  # 10 minutes


class AuthFailureTracker:
    """Thread-safe sliding-window counter of rejected (401) requests.

    Pure logic, no I/O: record() just appends a timestamp (pruning anything
    older than window_s), summary() prunes again and reports the current
    count plus first/last timestamps in the window. Safe to instantiate
    multiple independent trackers in tests without touching any shared state.
    """

    def __init__(self, window_s: float = WINDOW_SECONDS_DEFAULT):
        self.window_s = window_s
        self._lock = threading.Lock()
        self._events: list[float] = []  # timestamps of rejected requests, ascending
        self._last_ts: Optional[float] = None  # last rejection ever seen (outside window too)

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_s
        # _events is append-ordered (ascending timestamps), so we can drop from the
        # front instead of rebuilding the whole list every call.
        i = 0
        n = len(self._events)
        while i < n and self._events[i] < cutoff:
            i += 1
        if i:
            del self._events[:i]

    def record(self, ts: Optional[float] = None) -> None:
        """Record one rejected (401) request. Never raises."""
        now = time.time() if ts is None else ts
        with self._lock:
            self._events.append(now)
            self._last_ts = now
            self._prune(now)

    def summary(self, ts: Optional[float] = None) -> dict:
        """Return {"auth_fail_10m": int, "auth_fail_last_ts": float|None}.

        auth_fail_10m counts rejections within the sliding window as of `ts`
        (defaults to time.time()). auth_fail_last_ts is the timestamp of the
        most recent rejection ever recorded (None if there has never been one),
        independent of the window, so an operator can see "how long ago" even
        after the burst has aged out of the 10-minute count.
        """
        now = time.time() if ts is None else ts
        with self._lock:
            self._prune(now)
            return {
                "auth_fail_10m": len(self._events),
                "auth_fail_last_ts": self._last_ts,
            }


# Module-level singleton used by main.py's middleware and /health route.
_TRACKER = AuthFailureTracker()

# Where the cockpit reads the same summary without an HTTP round-trip.
_STATS_FILE = Path(__file__).resolve().parent.parent / ".fleet" / "auth_stats.json"


def record_auth_failure(ts: Optional[float] = None) -> None:
    """Record one rejected request against the module singleton, then
    best-effort persist the updated summary to .fleet/auth_stats.json.
    Never raises -- called from the request path."""
    try:
        _TRACKER.record(ts)
    except Exception:
        pass
    write_snapshot()


def get_summary() -> dict:
    """Current sliding-window summary from the module singleton. Never raises;
    returns a zeroed summary on unexpected failure."""
    try:
        return _TRACKER.summary()
    except Exception:
        return {"auth_fail_10m": 0, "auth_fail_last_ts": None}


def write_snapshot() -> None:
    """Best-effort atomic write of the current summary to .fleet/auth_stats.json
    (tmp file + os.replace, same pattern used elsewhere in this repo for
    crash-safe sidecar files). Any failure (missing dir, permissions, full
    disk) is swallowed -- this must never break request handling."""
    try:
        payload = get_summary()
        _STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(_STATS_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, str(_STATS_FILE))
    except Exception:
        pass
