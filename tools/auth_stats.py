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

Per-IP breakdown (request origin): record() optionally takes the caller's
identity IP (derived by tools.security.derive_identity -- the SAME helper the
unlock gate uses, so this data lines up with what the unlock gate actually
saw). The breakdown is exposed only through the local .fleet/auth_stats.json
sidecar (write_snapshot()/_snapshot_payload()), never through get_summary()/
/health: /health is unauthenticated and reachable from the public internet,
and handing an outside observer our own view of client addresses would be a
net loss for no operational gain. get_summary()'s return shape is therefore
unchanged from before this breakdown existed.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

WINDOW_SECONDS_DEFAULT = 600.0  # 10 minutes

# Max number of distinct real IPs the per-IP breakdown tracks with their own
# bucket at once. Chosen as a small, human-scannable number: this data is read
# by an operator glancing at a sidecar file, not fed into any automated
# decision, so 50 rows is already more than anyone will read line by line, and
# it keeps the tracker's memory bounded (each bucket is just a list of floats)
# no matter how many distinct addresses show up -- e.g. a client rotating
# through source IPs, or IPv6 privacy addresses, cannot grow this without
# bound. Once at capacity, any newly-seen IP folds into a single "other"
# aggregate bucket instead of getting its own; a tracked IP's slot is freed
# automatically once all of its events age out of the window, so the cap
# self-heals rather than permanently locking in whichever 50 IPs showed up
# first.
IP_BUCKET_CAP_DEFAULT = 50

# Sentinel key for the overflow bucket. Deliberately not a value that could
# ever be a real IP string, so it can't collide with one.
_OTHER_IP_KEY = "__other__"


class AuthFailureTracker:
    """Thread-safe sliding-window counter of rejected (401) requests.

    Pure logic, no I/O: record() just appends a timestamp (pruning anything
    older than window_s), summary() prunes again and reports the current
    count plus first/last timestamps in the window. Safe to instantiate
    multiple independent trackers in tests without touching any shared state.

    Optionally also keeps a capped per-IP breakdown (see IP_BUCKET_CAP_DEFAULT
    above) of the same rejected requests, available via
    summary(include_ip_breakdown=True). This is additive: every field that
    existed before this breakdown was added keeps its exact name and meaning,
    and summary() with no arguments returns exactly what it always did.
    """

    def __init__(
        self,
        window_s: float = WINDOW_SECONDS_DEFAULT,
        ip_cap: int = IP_BUCKET_CAP_DEFAULT,
    ):
        self.window_s = window_s
        self.ip_cap = ip_cap
        self._lock = threading.Lock()
        self._events: list[float] = []  # timestamps of rejected requests, ascending
        self._last_ts: Optional[float] = None  # last rejection ever seen (outside window too)
        # Per-IP timestamps, ascending, one list per distinct real IP currently
        # within its cap slot (bounded to at most `ip_cap` keys -- see record()).
        self._ip_events: dict[str, list[float]] = {}
        # Overflow bucket: events from IPs seen after the cap was already full.
        self._other_events: list[float] = []

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

    @staticmethod
    def _prune_list(events: list[float], cutoff: float) -> None:
        """Same front-trim as _prune(), factored out so the per-IP lists and
        the overflow bucket can reuse it. Caller must hold self._lock."""
        i = 0
        n = len(events)
        while i < n and events[i] < cutoff:
            i += 1
        if i:
            del events[:i]

    def _prune_ip(self, now: float) -> None:
        """Prune every per-IP list and the overflow bucket. Dropping an IP's
        list entirely once it goes empty is what frees its cap slot for a
        future, previously-unseen IP. Caller must hold self._lock."""
        cutoff = now - self.window_s
        self._prune_list(self._other_events, cutoff)
        emptied = []
        for ip, events in self._ip_events.items():
            self._prune_list(events, cutoff)
            if not events:
                emptied.append(ip)
        for ip in emptied:
            del self._ip_events[ip]

    def record(self, ts: Optional[float] = None, ip: Optional[str] = None) -> None:
        """Record one rejected (401) request, optionally tagged with the
        caller's identity IP. Never raises.

        `ip` should be whatever tools.security.derive_identity() (or
        _parse_request()) resolved for this request -- the SAME derivation
        the unlock gate uses, so this data reflects the actual client the
        request came from rather than a second, possibly-divergent guess.
        An absent/unknown IP (None, "", or anything that fails str()) degrades
        to the empty-string placeholder bucket rather than raising or being
        dropped -- observability must never depend on the IP being resolvable.
        """
        now = time.time() if ts is None else ts
        try:
            norm_ip = "" if ip is None else str(ip)
        except Exception:
            norm_ip = ""
        with self._lock:
            self._events.append(now)
            self._last_ts = now
            self._prune(now)

            self._prune_ip(now)
            if norm_ip in self._ip_events:
                self._ip_events[norm_ip].append(now)
            elif len(self._ip_events) < self.ip_cap:
                self._ip_events[norm_ip] = [now]
            else:
                # At capacity and this IP doesn't already have a slot: fold it
                # into the overflow bucket instead of growing the dict further.
                self._other_events.append(now)

    def summary(self, ts: Optional[float] = None, include_ip_breakdown: bool = False) -> dict:
        """Return {"auth_fail_10m": int, "auth_fail_last_ts": float|None}.

        auth_fail_10m counts rejections within the sliding window as of `ts`
        (defaults to time.time()). auth_fail_last_ts is the timestamp of the
        most recent rejection ever recorded (None if there has never been one),
        independent of the window, so an operator can see "how long ago" even
        after the burst has aged out of the 10-minute count.

        When include_ip_breakdown=True, an additional "auth_fail_by_ip" key is
        included: {ip: count} for every IP currently holding a cap slot,
        pruned to the same window, plus an "__other__" entry (only present if
        non-zero) for the capped overflow bucket. Default is False so existing
        callers (get_summary(), used by the public /health route) keep
        returning exactly the same two-key shape they always have.
        """
        now = time.time() if ts is None else ts
        with self._lock:
            self._prune(now)
            result = {
                "auth_fail_10m": len(self._events),
                "auth_fail_last_ts": self._last_ts,
            }
            if include_ip_breakdown:
                self._prune_ip(now)
                by_ip = {ip: len(events) for ip, events in self._ip_events.items() if events}
                if self._other_events:
                    by_ip[_OTHER_IP_KEY] = len(self._other_events)
                result["auth_fail_by_ip"] = by_ip
            return result


# Module-level singleton used by main.py's middleware and /health route.
_TRACKER = AuthFailureTracker()

# Where the cockpit reads the same summary without an HTTP round-trip.
_STATS_FILE = Path(__file__).resolve().parent.parent / ".fleet" / "auth_stats.json"


def record_auth_failure(ts: Optional[float] = None, ip: Optional[str] = None) -> None:
    """Record one rejected request against the module singleton, then
    best-effort persist the updated summary to .fleet/auth_stats.json.
    Never raises -- called from the request path.

    `ip` is the caller's identity IP as derived by
    tools.security.derive_identity() -- pass it through unchanged; do not
    re-derive it here or anywhere else (see that function's docstring for
    why there must be exactly one derivation)."""
    try:
        _TRACKER.record(ts=ts, ip=ip)
    except Exception:
        pass
    write_snapshot()


def get_summary() -> dict:
    """Current sliding-window summary from the module singleton. Never raises;
    returns a zeroed summary on unexpected failure.

    Deliberately does NOT include the per-IP breakdown -- this is what feeds
    the public, unauthenticated /health route, and publishing the list of
    client addresses there would hand any outside observer our own view of
    them. For the per-IP breakdown see _snapshot_payload()/write_snapshot(),
    which only ever reaches the local .fleet/auth_stats.json sidecar."""
    try:
        return _TRACKER.summary()
    except Exception:
        return {"auth_fail_10m": 0, "auth_fail_last_ts": None}


def _snapshot_payload() -> dict:
    """Fuller summary, including the per-IP breakdown, for the LOCAL sidecar
    file only. Kept as a separate function from get_summary() -- which /health
    calls -- so the two can never accidentally converge back to the same
    payload; see get_summary()'s docstring for why that separation matters."""
    try:
        return _TRACKER.summary(include_ip_breakdown=True)
    except Exception:
        return {"auth_fail_10m": 0, "auth_fail_last_ts": None}


def write_snapshot() -> None:
    """Best-effort atomic write of the current summary (including the per-IP
    breakdown) to .fleet/auth_stats.json (tmp file + os.replace, same pattern
    used elsewhere in this repo for crash-safe sidecar files). Any failure
    (missing dir, permissions, full disk) is swallowed -- this must never
    break request handling."""
    try:
        payload = _snapshot_payload()
        _STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(_STATS_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, str(_STATS_FILE))
    except Exception:
        pass
