"""Token supply policy for the fleet socket route: when to capture, and when to just wait.

Two defects this module removes (design review 2026-10-14, ledger items 3 and 7):

1. A goal admitted while no token was in hand fell through to a SYNCHRONOUS tab open on the one
   sweep thread (up to ~210 s), froze status.json, and the watchdog then killed the Edge. Here a
   goal that would use a socket is HELD in the pending queue ("waiting for token") instead,
   with its reason visible in status.json. Nothing is opened and nothing blocks. A hold has a
   ceiling after which the historical tab path applies, so capability is never lost.

2. Captures were launched every ~2 minutes and mostly re-read the SAME old token (43 captures in
   2.5 h). A token's `exp` is known, so a capture that returned the same `exp` as the last one
   proves the page has nothing newer; asking again soon cannot help. `CapturePolicy` lengthens
   the interval for every consecutive same-`exp` result and for every failure (exponential,
   capped), and resets it as soon as a genuinely newer `exp` arrives.

A failed capture only ever backs off. It never touches the browser: no kill, no reset.

No credential is stored here; only `exp` (a timestamp) is remembered.
"""
from __future__ import annotations

import os
import threading
import time

#: First retry delay after a failure or a same-exp result. Doubles each time.
BASE_INTERVAL_S = float(os.environ.get("MCP_TOKEN_BASE_INTERVAL_S", "120"))
#: Ceiling of the backoff while a usable token is still in hand.
MAX_INTERVAL_S = float(os.environ.get("MCP_TOKEN_MAX_INTERVAL_S", "600"))
#: Ceiling of the backoff when NO usable token is in hand (workers are waiting on us).
MAX_INTERVAL_NO_TOKEN_S = float(os.environ.get("MCP_TOKEN_MAX_INTERVAL_NO_TOKEN_S", "300"))
#: How long a goal may be held waiting for a token before the tab path applies.
MAX_HOLD_S = float(os.environ.get("MCP_TOKEN_MAX_HOLD_S", "600"))
#: Own timeout for one capture helper (covers tier 2 plus the tier 3 worst case).
CAPTURE_TIMEOUT_S = float(os.environ.get("MCP_SOCKET_CAPTURE_TIMEOUT_S", "180"))


def exp_of(token) -> float:
    """The `exp` claim of a token (seconds since epoch), 0 when unreadable."""
    try:
        from relay.profile_token import _claims
        return float(_claims(token).get("exp") or 0)
    except Exception:
        return 0.0


class CapturePolicy:
    """Per-agent decision of when a capture may be started. Thread-safe, no I/O."""

    def __init__(self, base_s=None, max_s=None, max_no_token_s=None, now=time.time):
        self.base_s = BASE_INTERVAL_S if base_s is None else float(base_s)
        self.max_s = MAX_INTERVAL_S if max_s is None else float(max_s)
        self.max_no_token_s = (MAX_INTERVAL_NO_TOKEN_S if max_no_token_s is None
                               else float(max_no_token_s))
        self._now = now
        self._lock = threading.Lock()
        self._st = {}   # key -> {"exp", "streak", "fails", "next_at", "why"}

    def _state(self, key):
        return self._st.setdefault(key, {"exp": 0.0, "streak": 0, "fails": 0,
                                         "next_at": 0.0, "why": ""})

    def allow(self, key, now=None) -> bool:
        now = float(self._now() if now is None else now)
        with self._lock:
            return now >= self._state(key)["next_at"]

    def _delay(self, n, have_token):
        cap = self.max_s if have_token else self.max_no_token_s
        return min(cap, self.base_s * (2 ** max(0, n)))

    def on_attempt(self, key, now=None):
        """Called when a helper is launched: apply the base floor even if it never reports."""
        now = float(self._now() if now is None else now)
        with self._lock:
            st = self._state(key)
            st["next_at"] = max(st["next_at"], now + self.base_s)

    def on_success(self, key, token, have_token=True, now=None) -> float:
        """Record a capture result. Returns the delay until the next allowed capture."""
        now = float(self._now() if now is None else now)
        exp = exp_of(token)
        with self._lock:
            st = self._state(key)
            st["fails"] = 0
            if exp and st["exp"] and abs(exp - st["exp"]) < 1.0:
                st["streak"] += 1
                st["why"] = "same token expiry returned %d time(s)" % st["streak"]
            else:
                st["streak"] = 0
                st["why"] = ""
            st["exp"] = exp or st["exp"]
            delay = self._delay(st["streak"], have_token) if st["streak"] else self.base_s
            st["next_at"] = now + delay
            return delay

    def on_failure(self, key, have_token=False, why="", now=None) -> float:
        now = float(self._now() if now is None else now)
        with self._lock:
            st = self._state(key)
            st["fails"] += 1
            st["why"] = (why or "capture failed")[:160]
            delay = self._delay(st["fails"] - 1, have_token)
            st["next_at"] = now + delay
            return delay

    def snapshot(self, key, now=None) -> dict:
        now = float(self._now() if now is None else now)
        with self._lock:
            st = dict(self._state(key))
        return {"fails": st["fails"], "same_exp_streak": st["streak"],
                "next_capture_in_s": max(0, int(st["next_at"] - now)), "why": st["why"]}


class TokenHold:
    """Decides whether a pending goal should wait for a token instead of opening a tab."""

    def __init__(self, max_hold_s=None, now=time.time):
        self.max_hold_s = MAX_HOLD_S if max_hold_s is None else float(max_hold_s)
        self._now = now
        self._lock = threading.Lock()
        self._since = {}      # goal name -> first time held
        self._last = {}       # summary for status.json

    def hold_reason(self, name, route, agent_url, wants_socket=True) -> str:
        """'' = proceed (socket is ready, or a tab is legitimately needed); else the reason."""
        now = float(self._now())
        try:
            if not wants_socket or not route.open():
                self.release(name)
                return ""
            if route.ready(agent_url):
                self.release(name)
                return ""
        except Exception:
            return ""
        with self._lock:
            since = self._since.setdefault(name, now)
            waited = now - since
            if waited > self.max_hold_s:
                self._last = {"waiting": len(self._since), "expired": name,
                              "reason": "token wait exceeded %.0fs; tab path allowed"
                                        % self.max_hold_s}
                self._since.pop(name, None)
                return ""
            reason = "waiting for token (%.0fs)" % waited
            self._last = {"waiting": len(self._since), "oldest_s": int(
                now - min(self._since.values())), "reason": reason}
            return reason

    def release(self, name):
        with self._lock:
            self._since.pop(name, None)
            if not self._since and self._last.get("waiting"):
                self._last = {}

    def summary(self) -> dict:
        with self._lock:
            return dict(self._last)


def goal_wants_socket(worker, resume_is_url=False) -> bool:
    """Mirror of the transport decision in RelayWorker.attach: a goal bound to a page URL, or
    one the transport policy sends to a tab, must not be held for a token."""
    if resume_is_url:
        return False
    try:
        from relay.transport_policy import SOCKET, choose
        return choose(getattr(worker, "goal", ""),
                      kind=getattr(worker, "task_kind", "") or "") == SOCKET
    except Exception:
        return True


_HOLD = None


def hold() -> TokenHold:
    global _HOLD
    if _HOLD is None:
        _HOLD = TokenHold()
    return _HOLD


def status_block() -> dict:
    """For status.json: why goals are waiting for a token (empty dict when none are)."""
    try:
        return hold().summary()
    except Exception:
        return {}
