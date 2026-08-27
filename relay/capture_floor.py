"""No capture may be asked for again within a floor, whatever capture is plugged in.

WHAT THIS STOPS. The route decides it needs a refresh by comparing the token's remaining life
against a margin, and the margin is 1500 seconds. The tenant issues tokens with a median life
of 52 minutes but a minimum of 14, so when a short one comes back `needs_refresh` is true the
instant the capture meant to satisfy it finishes -- and the route captures again. Measured at
3.8 captures a minute, each holding a page for thirty-five seconds, which is to say the browser
was never WITHOUT one. It ran for weeks. Every individual capture was correct.

Capturing faster cannot lengthen a token, so the loop buys nothing at all.

WHY HERE AND NOT IN THE CAPTURE. relay/profile_token.py already refuses to open a page when it
answered moments ago, and that was not enough for two reasons. It is one capture_fn among
several -- with MCP_CAPTURE_LIGHT off the route gets capture_via_tab, which has no floor at all
-- and a floor that lives in the callee protects only the callees that remember to have one.
The spin is in the CALLER's loop, so the floor belongs between the two, where every
implementation present and future passes through.

WHY NOT IN socket_route.py, WHICH IS WHERE THE LOOP ACTUALLY IS. That module is frozen. It
takes its capture_fn from the caller, and a wrapper applied at that seam covers the same ground
without editing it: the route cannot ask for a capture except through what it was handed.

WHAT A FLOORED CALL RETURNS. The previous answer, not an error. A raise would reach
`note_failure` and count against the circuit breaker for something that is not a failure --
the route asked twice quickly and got the same true answer twice, which is what it would have
got from a capture anyway, several hundred megabytes later.
"""
from __future__ import annotations

import os
import threading
import time

#: How soon the same surface may be captured again. Longer than any burst of admissions at the
#: start of a run, and far shorter than the shortest token observed (14 minutes), so a run
#: whose margin IS satisfiable never notices this exists.
MIN_CAPTURE_INTERVAL_S = float(os.environ.get("MCP_MIN_CAPTURE_INTERVAL_S", "120"))

#: Below this there is nothing worth serving: the token would expire during the turn it was
#: handed to, and an expensive capture is the honest answer.
SERVE_FLOOR_S = float(os.environ.get("MCP_TOKEN_SERVE_FLOOR_S", "90"))


def _token_life_s(token, now=None):
    try:
        from relay.profile_token import token_life_s
        return token_life_s(token, now=now)
    except Exception:
        return 0.0


class CaptureFloor:
    """A capture_fn wrapper that will not run the real one more often than the floor.

    Stateful and per-surface, because two agent surfaces are two independent tokens and
    throttling one on the other's account would starve it.
    """

    def __init__(self, inner, interval_s=None, serve_floor_s=None, now=time.time, log=None):
        self._inner = inner
        self.interval_s = MIN_CAPTURE_INTERVAL_S if interval_s is None else float(interval_s)
        self.serve_floor_s = SERVE_FLOOR_S if serve_floor_s is None else float(serve_floor_s)
        self._now = now
        self._log = log or (lambda m: print(m, flush=True))
        self._lock = threading.Lock()
        self._last = {}                 # key -> {"at", "token", "template"}
        self._served = 0                # how many asks were answered without a capture
        self._captured = 0              # how many reached the real capture
        self._explained = set()

    # -- the capture_fn contract -----------------------------------------------------------
    def __call__(self, context, agent_url):
        key = agent_url or ""
        now = self._now()
        with self._lock:
            entry = self._last.get(key)
        if entry is not None:
            waited = now - entry["at"]
            if waited < self.interval_s and \
                    _token_life_s(entry["token"], now=now) >= self.serve_floor_s:
                with self._lock:
                    self._served += 1
                self._explain(key, waited)
                return entry["token"], entry["template"]
        # THE ONE POINT EVERY CAPTURE PASSES THROUGH, which is why the fact of a capture is
        # recorded here. The cockpit used to judge sign-in and agent binding from the
        # browser's tab list; under the socket route there are no tabs, so it read the
        # emptiness as health -- green because nothing was there, and equally green with an
        # expired sign-in. What a capture establishes is written to a file a window can read.
        try:
            token, template = self._inner(context, agent_url)
        except Exception as exc:
            self._record_failure(exc, agent_url)
            raise
        with self._lock:
            self._last[key] = {"at": self._now(), "token": token, "template": template}
            self._captured += 1
        self._record_success(token, template, agent_url)
        return token, template

    @staticmethod
    def _record_success(token, template, agent_url):
        """Never raises: a status write must not be able to fail a capture."""
        try:
            from relay.capture_status import record_success
            record_success(token, template, agent_url)
        except Exception:
            pass

    @staticmethod
    def _record_failure(exc, agent_url):
        try:
            from relay.capture_status import record_failure
            record_failure(exc, agent_url)
        except Exception:
            pass

    def _explain(self, key, waited):
        """Say it ONCE per surface. The condition is structural, and a line per suppressed
        capture at 3.8 a minute is a log nobody reads -- which is how the original defect
        survived: everything was in the log and the log was too long to notice a rate in."""
        if key in self._explained:
            return
        self._explained.add(key)
        self._log(
            "[capture_floor] the route asked again %.0fs after a capture, sooner than the %.0fs "
            "floor, so the token already in hand was returned instead. This means the refresh "
            "margin is longer than the token this tenant issues; capturing faster cannot "
            "lengthen a token." % (waited, self.interval_s))

    def stats(self) -> dict:
        with self._lock:
            return {"captured": self._captured, "served": self._served,
                    "surfaces": len(self._last), "interval_s": self.interval_s}


def floored(capture_fn, **kw):
    """Wrap `capture_fn`. Returns it unchanged if the floor is switched off."""
    raw = os.environ.get("MCP_CAPTURE_FLOOR", "1").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return capture_fn
    return CaptureFloor(capture_fn, **kw)
