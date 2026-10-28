"""Token supply policy for the fleet socket route: when to capture, and when to just wait.

Two defects this module removes (design review 2026-10-14, ledger items 3 and 7):

1. A goal admitted while no token was in hand fell through to a SYNCHRONOUS tab open on the one
   sweep thread (up to ~210 s), froze status.json, and the watchdog then killed the Edge. Here a
   goal is HELD in the pending queue ("waiting for token", or "socket route unavailable" when
   the route is shut) instead, with its reason visible in status.json. Nothing is opened and
   nothing blocks. THE HOLD HAS NO CEILING: Copilot never runs in a browser tab (owner rule,
   relay/no_tab.py), so there is no tab path to fall back to after any amount of waiting. The
   capture keeps being retried, paced by CapturePolicy; the route is reopened by
   relay/route_reopen.py.

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
#: Ceiling of the backoff when NO usable token is in hand (workers are waiting on us). 120 s:
#: a goal is waiting for this capture, so the wait between two attempts must stay short. The
#: 2026-10-14 freeze showed what a long ceiling costs when nothing else retries.
MAX_INTERVAL_NO_TOKEN_S = float(os.environ.get("MCP_TOKEN_MAX_INTERVAL_NO_TOKEN_S", "120"))
#: First retry delay after a failure while NO usable token is in hand. Doubles up to the ceiling
#: above (30, 60, 120, 120 ...), so a transient failure is retried quickly and a persistent one
#: is retried every two minutes for as long as a goal is waiting. Never longer.
NO_TOKEN_BASE_S = float(os.environ.get("MCP_TOKEN_NO_TOKEN_BASE_S", "30"))
#: Own timeout for one capture helper: a page load plus the wait for the page's own token
#: request (profile_token.LIGHT_TIMEOUT_S, 30 s), with room for a cold Edge (a cold capture was
#: measured at 77-134 s on 2026-10-14). There is no longer a real-turn stage in a capture.
CAPTURE_TIMEOUT_S = float(os.environ.get("MCP_SOCKET_CAPTURE_TIMEOUT_S", "180"))

#: Fraction of a token's ORIGINAL life at or below which a pre-emptive refresh is due. Replaces
#: the fleet's fixed 1500 s margin for the *pre-expiry* decision only (the route still keeps its
#: own fixed margin, which a test pins above the longest turn). A ratio scales with the token:
#: a 14-minute token and a 52-minute token both refresh at the same fraction of life left, so a
#: short token does NOT sit permanently inside a fixed margin and spin a capture every few
#: seconds (the 3.8/min defect). 0.20 of a 58-min life is ~11.6 min of head-room.
PREEMPT_REFRESH_FRACTION = float(os.environ.get("MCP_TOKEN_PREEMPT_FRACTION", "0.20"))
#: Half-width of the per-key jitter band around that fraction. The whole fleet shares one `exp`,
#: so without this every worker crosses the threshold in the same second and they all refuse
#: together (the 2026-10-27 16:00 burst: 498 refusals in one hour). A deterministic per-key
#: offset in [-BAND, +BAND] spreads the crossings so captures stagger instead of stampeding.
PREEMPT_JITTER_BAND = float(os.environ.get("MCP_TOKEN_PREEMPT_JITTER", "0.08"))


def _key_jitter(key, band) -> float:
    """A stable offset in [-band, +band] derived from the key, so a given agent always lands at
    the same point in the band (no flapping) but different agents land at different points."""
    import hashlib
    h = int(hashlib.sha256(str(key or "").encode("utf-8")).hexdigest()[:8], 16)
    frac = (h % 10000) / 10000.0        # [0, 1)
    return (frac * 2.0 - 1.0) * float(band)


def exp_of(token) -> float:
    """The `exp` claim of a token (seconds since epoch), 0 when unreadable."""
    try:
        from relay.profile_token import _claims
        return float(_claims(token).get("exp") or 0)
    except Exception:
        return 0.0


class CapturePolicy:
    """Per-agent decision of when a capture may be started. Thread-safe, no I/O."""

    def __init__(self, base_s=None, max_s=None, max_no_token_s=None, now=time.time,
                 no_token_base_s=None):
        self.base_s = BASE_INTERVAL_S if base_s is None else float(base_s)
        self.max_s = MAX_INTERVAL_S if max_s is None else float(max_s)
        self.max_no_token_s = (MAX_INTERVAL_NO_TOKEN_S if max_no_token_s is None
                               else float(max_no_token_s))
        #: Base of the failure backoff while no token is in hand. None = same as `base_s`.
        self.no_token_base_s = (self.base_s if no_token_base_s is None
                                else float(no_token_base_s))
        #: Pre-expiry refresh trigger: a fraction of the token's original life, with a per-key
        #: jitter band so the shared-`exp` fleet does not all cross it at once. See the module
        #: constants of the same name.
        self.preempt_fraction = PREEMPT_REFRESH_FRACTION
        self.preempt_band = PREEMPT_JITTER_BAND
        self._now = now
        self._lock = threading.Lock()
        # key -> {"exp", "streak", "fails", "next_at", "set_at", "why", "attempts", "last_error",
        #         "last_error_at", "last_ok_at", "force"}
        self._st = {}

    def _state(self, key):
        return self._st.setdefault(key, {"exp": 0.0, "streak": 0, "fails": 0,
                                         "next_at": 0.0, "set_at": 0.0, "why": "",
                                         "attempts": 0, "last_error": "",
                                         "last_error_at": 0.0, "last_ok_at": 0.0,
                                         "force": False, "life0": 0.0})

    def arm_expired(self, key):
        """A turn was just refused with an EXPIRED token: the shared `exp` really is past, so the
        next capture must not be held back by the floor or by a same-exp/failure backoff that was
        set when the token still had life. Opens a one-shot window that `allow` honours exactly
        once; a genuine capture then resets the streak and the window closes on its own.

        This cannot become a spin: one expired response arms at most one immediate capture, and
        that capture either installs a newer `exp` (streak resets) or fails (backoff resumes from
        `on_failure`)."""
        with self._lock:
            self._state(key)["force"] = True

    def allow(self, key, now=None, held=False) -> bool:
        """May a capture start now?

        `held=True` means a goal is waiting on this capture. Whatever interval was computed
        (a same-exp streak can reach `max_s`), a held goal never waits longer than the no-token
        ceiling counted from when that interval was set: the long same-exp backoff is for an
        idle fleet with a token in hand, not for one that is frozen on it."""
        now = float(self._now() if now is None else now)
        with self._lock:
            st = self._state(key)
            if st.get("force"):
                # A proven expiry armed this: bypass the floor and the backoff exactly once,
                # then close the window so the normal pacing governs every later attempt.
                st["force"] = False
                return True
            due = st["next_at"]
            if held and st["set_at"]:
                due = min(due, st["set_at"] + self.max_no_token_s)
            return now >= due

    def due_for_preemptive_refresh(self, key, token=None, now=None,
                                   fraction=None, band=None) -> bool:
        """Should a refresh be kicked BEFORE the token expires?

        Replaces the fleet's fixed 1500 s margin for the pre-expiry decision. The whole fleet
        shares one `exp`; a fixed margin makes every worker cross it in the same second, so they
        refuse together. Here the trigger is a FRACTION of the token's original life, offset by a
        deterministic per-key jitter so the crossings stagger.

        True when  remaining / life0  <=  fraction + jitter(key).

        Why this cannot spin on a short token (the fixed-margin 3.8/min defect): the test is a
        ratio, so right after a capture  remaining ~= life0  and the ratio ~= 1.0, far above any
        threshold in (0, 1); it only becomes due once the token has actually spent (1 - thr) of
        its life, whatever that life is. A 14-min token and a 52-min token both get head-room
        proportional to their own life instead of a 25-min margin that a 14-min token can never
        satisfy. With no readable life0 yet, it returns False (nothing proven to refresh)."""
        now = float(self._now() if now is None else now)
        exp = exp_of(token) if token else 0.0
        with self._lock:
            st = self._state(key)
            exp = exp or st.get("exp") or 0.0
            life0 = float(st.get("life0") or 0.0)
        if exp <= 0.0 or life0 <= 0.0:
            return False
        remaining = exp - now
        if remaining <= 0.0:
            return True  # already expired: certainly due
        frac = self.preempt_fraction if fraction is None else float(fraction)
        bnd = self.preempt_band if band is None else float(band)
        thr = frac + _key_jitter(key, bnd)
        thr = min(0.95, max(0.01, thr))     # keep the threshold a sane fraction
        return (remaining / life0) <= thr

    def _delay(self, n, have_token):
        if have_token:
            return min(self.max_s, self.base_s * (2 ** max(0, n)))
        return min(self.max_no_token_s, self.no_token_base_s * (2 ** max(0, n)))

    def on_attempt(self, key, now=None):
        """Called when a helper is launched: apply a floor even if it never reports.

        The floor is the SHORTER of the two bases, so a failing no-token capture is not held
        back longer than its own backoff would have held it."""
        now = float(self._now() if now is None else now)
        with self._lock:
            st = self._state(key)
            st["attempts"] += 1
            floor = min(self.base_s, self.no_token_base_s)
            st["next_at"] = max(st["next_at"], now + floor)
            st["set_at"] = now

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
            # Original life of THIS token (exp - now at capture), the denominator the ratio uses.
            # Recorded only for a genuinely newer exp; a same-exp return keeps the first life0 so
            # the ratio keeps shrinking toward 0 as the shared token ages.
            if exp and not st["streak"]:
                st["life0"] = max(0.0, exp - now)
            delay = self._delay(st["streak"], have_token) if st["streak"] else self.base_s
            st["next_at"] = now + delay
            st["set_at"] = now
            st["last_ok_at"] = now
            return delay

    def on_failure(self, key, have_token=False, why="", now=None) -> float:
        now = float(self._now() if now is None else now)
        with self._lock:
            st = self._state(key)
            st["fails"] += 1
            st["why"] = (why or "capture failed")[:160]
            st["last_error"] = st["why"]
            st["last_error_at"] = now
            delay = self._delay(st["fails"] - 1, have_token)
            st["next_at"] = now + delay
            st["set_at"] = now
            return delay

    def snapshot(self, key, now=None) -> dict:
        now = float(self._now() if now is None else now)
        with self._lock:
            st = dict(self._state(key))
        return {"fails": st["fails"], "same_exp_streak": st["streak"],
                "next_capture_in_s": max(0, int(st["next_at"] - now)), "why": st["why"],
                "attempts": st["attempts"], "last_error": st["last_error"],
                "last_error_at": st["last_error_at"], "last_ok_at": st["last_ok_at"]}


def wait_reason(route, agent_url) -> str:
    """Why a goal cannot be bound to a socket right now, as the text status.json shows.

    "socket route unavailable" when the route is shut or switched off; "waiting for token" when
    the route is open but holds no usable token/template yet. There is no third answer: a tab is
    never an option (relay/no_tab.py)."""
    try:
        if not route.enabled:
            return ("socket route unavailable -- disabled (MCP_FLEET_SOCKET); no tab is ever "
                    "opened for a goal")
        if not route.open():
            return ("socket route unavailable -- %s; reopen probe every <=20s (no turn spent), "
                    "no tab is opened" % (
                        str(getattr(route, "closed_reason", "") or "closed")[:100]))
    except Exception:
        pass
    return "waiting for token"


#: A held goal that has not been looked at for this long is no longer held (it finished, was
#: cancelled, or was bound elsewhere). Every held goal is looked at on each sweep lap, so this
#: only ever drops a goal that the sweep stopped asking about.
HOLD_STALE_S = float(os.environ.get("MCP_TOKEN_HOLD_STALE_S", "60"))


class TokenHold:
    """Decides whether a goal must wait because the socket cannot serve it yet.

    Covers BOTH kinds of waiting goal: one still in the admission queue, and one that lost its
    socket and is waiting to be given a new driver. (The second kind was invisible: on
    2026-10-14 two such workers waited five hours with `token_wait` empty.)

    No ceiling and no tab: a goal waits here for as long as it takes the capture / the route
    repair to succeed. `since` is kept per goal only so status.json can show how long."""

    def __init__(self, now=time.time):
        self._now = now
        self._lock = threading.Lock()
        self._since = {}      # goal name -> first time held
        self._seen = {}       # goal name -> last time it was asked about
        self._last = {}       # summary for status.json

    def _refresh_summary(self, now, reason=""):
        for n in [n for n, t in self._seen.items() if now - t > HOLD_STALE_S]:
            self._seen.pop(n, None)
            self._since.pop(n, None)
        if not self._since:
            self._last = {}
            return
        self._last = {"waiting": len(self._since),
                      "oldest_s": int(now - min(self._since.values())),
                      "reason": reason or self._last.get("reason", "")}

    def hold_reason(self, name, route, agent_url, wants_socket=True) -> str:
        """'' = proceed (the route can serve a socket now); else why the goal waits.

        `wants_socket` is accepted for old callers and ignored: every goal is a socket goal."""
        now = float(self._now())
        try:
            if route.open() and route.ready(agent_url):
                self.release(name)
                return ""
            base = wait_reason(route, agent_url)
        except Exception:
            return ""
        return self.hold_text(name, base)

    def hold_text(self, name, base) -> str:
        """Register `name` as held with an explicit reason (e.g. 'waiting for MCP server ...')
        so status.json `token_wait` counts it. Returns the reason with the wait time."""
        now = float(self._now())
        with self._lock:
            since = self._since.setdefault(name, now)
            self._seen[name] = now
            waited = now - since
            reason = "%s (%.0fs)" % (base, waited)
            self._refresh_summary(now, reason)
            return reason

    def release(self, name):
        with self._lock:
            self._since.pop(name, None)
            self._seen.pop(name, None)
            if not self._since and self._last.get("waiting"):
                self._last = {}

    def summary(self) -> dict:
        with self._lock:
            self._refresh_summary(float(self._now()))
            return dict(self._last)


_HOLD = None


def hold() -> TokenHold:
    global _HOLD
    if _HOLD is None:
        _HOLD = TokenHold()
    return _HOLD


class _CaptureState:
    """What the no-turn capture has done, for status.json. Process-wide on purpose: the route
    (and its capture manager) is discarded and rebuilt on every route reopen, and the totals
    must survive that or "attempts: 0" would read as "never tried" right after a reopen.

    Holds counters and one short error string. No credential, no token."""

    def __init__(self, now=time.time):
        self._now = now
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.attempts = 0
            self.failures = 0
            self.successes = 0
            self.last_error = ""
            self.last_error_at = 0.0
            self.last_ok_at = 0.0
            self.next_at = 0.0
            self.inflight = False

    def attempt(self):
        with self._lock:
            self.attempts += 1
            self.inflight = True

    def ok(self, next_in_s=0.0):
        now = float(self._now())
        with self._lock:
            self.successes += 1
            self.last_ok_at = now
            self.inflight = False
            self.next_at = now + float(next_in_s or 0.0)

    def fail(self, error, next_in_s=0.0):
        now = float(self._now())
        with self._lock:
            self.failures += 1
            self.last_error = str(error or "capture failed")[:200]
            self.last_error_at = now
            self.inflight = False
            self.next_at = now + float(next_in_s or 0.0)

    def done(self):
        """The helper ended without a verdict (discarded install): it is no longer in flight."""
        with self._lock:
            self.inflight = False

    def set_next(self, next_in_s):
        with self._lock:
            self.next_at = float(self._now()) + float(next_in_s or 0.0)

    def snapshot(self) -> dict:
        now = float(self._now())
        with self._lock:
            return {
                "attempts": self.attempts, "failures": self.failures,
                "successes": self.successes, "inflight": self.inflight,
                "last_error": self.last_error,
                "last_error_age_s": (int(now - self.last_error_at)
                                     if self.last_error_at else None),
                "last_ok_age_s": int(now - self.last_ok_at) if self.last_ok_at else None,
                "next_attempt_in_s": (max(0, int(self.next_at - now)) if self.next_at else 0),
            }

    def unresolved_failure(self) -> bool:
        """A capture failed and has not been followed by a success: the reason is still the live
        state of the token supply. Used so the reason surfaces in status.json even on a lap when
        no goal happens to be held -- a silent WAIT was the whole defect (a capture that raised
        NotAnAgentSurface left the goal waiting with nothing said)."""
        with self._lock:
            return bool(self.last_error and self.last_error_at > self.last_ok_at
                        and not self.inflight)


_CAPTURE = _CaptureState()


def capture_state() -> _CaptureState:
    return _CAPTURE


def status_block() -> dict:
    """For status.json `token_wait`: why goals are waiting for a token, and what the capture that
    is supposed to end the wait has done (attempts, last error, next attempt). An empty dict when
    no goal is waiting -- the cockpit reads a non-empty block as "goals are held"."""
    try:
        blk = hold().summary()
        cap = capture_state()
        if blk:
            blk = dict(blk)
            blk["capture"] = cap.snapshot()
            return blk
        # No goal is held this instant, but if the last capture FAILED and nothing has
        # recovered since, the reason is still live and must be visible -- otherwise a capture
        # that raised (NotAnAgentSurface, a crashed helper) leaves the fleet in a silent WAIT
        # with `token_wait` empty, which is exactly the state this change removes.
        if cap.unresolved_failure():
            return {"waiting": 0, "reason": "last capture failed; no token installed",
                    "capture": cap.snapshot()}
        return blk
    except Exception:
        return {}
