"""Let a route that closed over a passing fault open again, without spending a turn to find out.

WHY THE ROUTE CLOSES AT ALL, AND WHY IT NEVER REOPENED. Three consecutive fallbacks trip a
one-way breaker. socket_route.py states the reason for the one-wayness plainly: "a backend that
has started refusing should not be asked again and again AT THE PRICE OF A FAILED TURN EACH
TIME." That is the whole objection, and it is a good one. Every proposal that answers it with
"try a worker on the socket and see" pays exactly the cost it names.

WHAT IT COSTS TO STAY SHUT. Measured 2026-08-27 at 12:03: an upstream proxy refused the
websocket upgrade with HTTP 502 for about a minute, three workers hit it, the route closed, and
the remaining forty minutes of an hour-long run went over tabs -- roughly 900 MB of browser
instead of 390. The blockage lasted a minute; the consequence lasted the run.

THE TWO THINGS THAT MAKE A REOPEN SAFE HERE

  1. ASK WITHOUT SPENDING A TURN. A websocket handshake answers "is the transport reachable"
     on its own: connect, and close. No chat frame, no turn, no worker waiting on it. So the
     objection's price is not paid, and the probe can be repeated cheaply.

     ON ITS OWN THREAD, FOR TWO SEPARATE REASONS. websocket_connect starts an event loop, and
     the caller here is the fleet's admission loop, which runs inside `with sync_playwright()`
     -- a thread that already owns one. Called there it raises "Cannot run the event loop
     while another loop is running", which an `except Exception: return False` would have
     filed as evidence that the transport is down. It is not evidence of anything; it is the
     probe failing to run. And a handshake against a dead proxy takes the full timeout, which
     is time the admission loop would spend admitting nobody. So consider() starts a probe and
     returns; a later pass reads the answer.

  2. ONLY FOR THE FAULTS THAT WERE TRANSPORT. transport_policy already sorts a fault into
     route / task / unknown, and the close carries the reason that tripped it. Across every
     close on record -- five -- four classify as `route` and the fifth was a deliberate
     forced-failure test. A close whose reason is a task or is unread stays shut, which is
     exactly the backend-is-refusing case the one-way rule was written for.

WHAT A HANDSHAKE DOES NOT PROVE. That the backend will accept a chat frame. A route closed by
"the backend declined the request: InvalidRequest" would very likely handshake fine, and that
is why (2) is not optional: the reason gate keeps those shut whatever the socket says.

A SUPPORTING SIGNAL, NOT A SUBSTITUTE. If some other agent surface has completed a socket turn
since the close, the transport is demonstrably up -- better evidence than a handshake, because
a whole turn went through. It is used to skip straight to the reopen when it is available, and
it is available only when a side agent happened to be working, so it cannot be relied on.

BACKOFF, BECAUSE AN UPSTREAM OUTAGE IS NOT A BLIP. Each failed probe doubles the wait. A proxy
that is down for an hour is asked eight times, not eight hundred.

THIS LIVES OUTSIDE socket_route.py, WHICH IS FROZEN. It needs nothing from inside: the route
already exposes status(), and relay_fleet already owns reset_socket_route(), which discards the
closed route so the next caller builds a fresh one. The mechanism for reopening existed; only
the decision of when did not.
"""
from __future__ import annotations

import os
import threading
import time
import uuid

#: How long after a close before the first probe. Long enough that the incident which closed
#: the route has a chance to end -- the one on record lasted about a minute.
FIRST_WAIT_S = float(os.environ.get("MCP_ROUTE_REOPEN_WAIT_S", "120"))

#: Each failed probe doubles the wait, up to this. An upstream outage should be asked about
#: occasionally, not continuously.
MAX_WAIT_S = float(os.environ.get("MCP_ROUTE_REOPEN_MAX_WAIT_S", "900"))

#: How long a handshake may take before it counts as a failure. Generous: the fault this exists
#: for is a proxy refusing an upgrade, which fails fast, and a slow success is still a success.
PROBE_TIMEOUT_S = float(os.environ.get("MCP_ROUTE_REOPEN_PROBE_S", "15"))


class ReopenPolicy:
    """Decides whether a closed route may be tried again, and remembers what it has tried.

    Stateful because backoff is: the first probe comes after FIRST_WAIT_S, and each failure
    pushes the next one out. Constructed once per run alongside the route.
    """

    def __init__(self, now=time.time, log=None, connect=None, probe=None, spawn=None):
        self._now = now
        self._log = log or (lambda m: print(m, flush=True))
        self._connect = connect
        self._probe = probe                    # injected for tests; None means the real one
        # HOW THE PROBE GETS OFF THIS THREAD. A test passes a spawn that runs the work at
        # once, so one consider() covers a whole probe cycle without sleeping; production
        # passes nothing and gets a thread.
        self._spawn = spawn or _thread_spawn
        self.closed_at = None                  # when this policy first saw the route shut
        self.next_probe_at = None
        self.wait_s = FIRST_WAIT_S
        self.probes = 0
        self.reopens = 0
        self._explained = False
        self._inflight = False                 # a probe is running on another thread
        self._result = None                    # (reachable, how) it left behind
        self._lock = threading.Lock()
        # THE TURN COUNT AT THE MOMENT THE CLOSE WAS SEEN. It lives here rather than on the
        # route because the route object is discarded on reopen, and because it must be
        # taken when the close is noticed -- see _consider.
        self._turns_at_close = None

    # -- the decision ----------------------------------------------------------------------
    def consider(self, route) -> bool:
        """True if the route was reopened.

        Never raises, and returns promptly: any handshake it starts runs on another thread,
        which is what makes it safe to call from the fleet's admission loop.
        """
        try:
            return self._consider(route)
        except Exception as exc:
            self._log("[reopen] gave up on this pass: %s: %s"
                      % (type(exc).__name__, str(exc)[:100]))
            return False

    def _consider(self, route) -> bool:
        status = route.status() if hasattr(route, "status") else {}
        if status.get("open") or not status.get("closed_reason"):
            self._reset()
            return False

        reason = str(status.get("closed_reason") or "")
        if not reopenable(reason):
            self._explain_once(
                "[reopen] the route is shut for a reason that is not transport, so it stays "
                "shut: %s" % reason[:110])
            return False

        now = self._now()
        if self.closed_at is None:
            self.closed_at = now
            self.next_probe_at = now + self.wait_s
            # BASELINE NOW, NOT AT PROBE TIME. Taken at the first probe, it was read in the
            # same call that set it, so the count could never have moved and the supporting
            # signal could not fire on the probe it was meant to inform -- the whole window
            # it exists to observe is between this line and that one.
            self._turns_at_close = _turns(route)
            self._log("[reopen] the route closed over a transport fault; it will be probed in "
                      "%.0fs WITHOUT spending a turn" % self.wait_s)
            return False

        # AN ANSWER FROM AN EARLIER PASS, IF ONE CAME BACK. Read before deciding whether to
        # start another, so a probe still in flight is never joined by a second.
        with self._lock:
            done, self._result = self._result, None
        if done is not None:
            return self._settle(now, done)
        if self._inflight or now < (self.next_probe_at or 0):
            return False

        self.probes += 1
        self._inflight = True
        self._spawn(lambda: self._run_probe(route))
        return False

    def _run_probe(self, route):
        """Off the caller's thread. Leaves an answer behind; never raises into the runner."""
        try:
            out = self._reachable(route)
        except Exception as exc:
            # NOT EVIDENCE THE TRANSPORT IS DOWN -- evidence the probe did not run. It backs
            # off like a failure, because retrying a broken probe quickly helps nobody, but
            # it says which of the two happened, because those are different repairs.
            out = (False, "the probe itself failed: %s" % type(exc).__name__)
        with self._lock:
            self._result = out
        self._inflight = False

    def _settle(self, now, result):
        ok, how = result
        if not ok:
            self.wait_s = min(self.wait_s * 2, MAX_WAIT_S)
            self.next_probe_at = now + self.wait_s
            self._log("[reopen] probe %d says the transport is still down (%s); next in %.0fs"
                      % (self.probes, how, self.wait_s))
            return False

        self._log("[reopen] probe %d says the transport is back (%s) -- reopening the route"
                  % (self.probes, how))
        self.reopens += 1
        self._reset()
        return True

    def _reset(self):
        self.closed_at = None
        self.next_probe_at = None
        self.wait_s = FIRST_WAIT_S
        self._explained = False
        self._turns_at_close = None
        with self._lock:
            self._result = None

    def _explain_once(self, message):
        if self._explained:
            return
        self._explained = True
        self._log(message)

    # -- the evidence ----------------------------------------------------------------------
    def _reachable(self, route):
        """(reachable, how). A completed turn elsewhere beats a handshake; either will do."""
        if turn_since_close(route, self._turns_at_close):
            return True, "another surface completed a socket turn since the close"
        if self._probe is not None:
            return bool(self._probe(route)), "handshake"
        return handshake(route, connect=self._connect,
                         timeout_s=PROBE_TIMEOUT_S, log=self._log), "handshake"


def reopenable(reason: str) -> bool:
    """Whether a close with this reason may ever be reconsidered.

    TRANSPORT ONLY. A close carrying a task-caused reason -- a consent card, an attachment, a
    turn that returned no text -- says something about the work, and a socket that handshakes
    will not change it. An UNREAD reason stays shut too: `unknown` means nobody has classified
    it, and treating the unclassified as transient is how a classifier gets quietly exonerated
    instead of extended.
    """
    try:
        from relay.transport_policy import classify_fallback
        return classify_fallback(reason or "") == "route"
    except Exception:
        return False


def turn_since_close(route, baseline) -> bool:
    """Has any socket turn completed since the route shut?

    THE SUPPORTING SIGNAL. A completed turn is better evidence than a handshake -- a whole
    request went through, not just an upgrade -- and it costs nothing, because it has already
    happened. It is only available when a side agent happened to be working at the time, so it
    can never be the mechanism.

    `baseline` is the count taken when the close was first seen. None means no baseline, which
    is not the same as no turns: with nothing to compare against, this says nothing.
    """
    if baseline is None:
        return False
    return _turns(route) > baseline


def _turns(route) -> int:
    """Socket turns completed on this route. note_success advances it; close_route does not."""
    try:
        return int((route.status() or {}).get("turns") or 0)
    except Exception:
        return 0

def handshake(route, connect=None, timeout_s=PROBE_TIMEOUT_S, log=None) -> bool:
    """Open a websocket to the same endpoint a turn would use, and close it. No frame is sent.

    THE URL COMES FROM Conversation.url_for_turn, not from a copy of it here. That method is
    where the query keys, the session key rule and the volatile-field handling live, and a
    second implementation would drift from the first in exactly the way that produces a probe
    reporting health about a URL production never uses.

    A route with no captured token cannot be probed and is not reported as down: there is
    nothing to ask with, which is a different fact from an unreachable transport.
    """
    say = log or (lambda _m: None)
    try:
        from relay.chathub import Conversation
        from relay.socket_route import websocket_connect
    except Exception:
        return False
    connect = connect or websocket_connect

    entry = None
    try:
        key = route._key()
        entry = dict(route._entries.get(key) or {})
    except Exception:
        entry = None
    token, template = (entry or {}).get("token"), (entry or {}).get("template")
    if not token or template is None:
        say("[reopen] nothing captured to probe with; leaving the route as it is")
        return False

    try:
        conv = Conversation(lambda: token, template=template)
        url = conv.url_for_turn(str(uuid.uuid4()))
    except Exception as exc:
        say("[reopen] could not build a probe url (%s); treating as not reachable"
            % type(exc).__name__)
        return False

    sock = None
    try:
        sock = connect(url, conv.headers(), timeout_s)
        return True
    except Exception:
        return False
    finally:
        try:
            if sock is not None:
                sock.close()
        except Exception:
            pass


def _thread_spawn(fn):
    """Run fn on a daemon thread. Daemon, because a probe must never hold a run open."""
    threading.Thread(target=fn, name="route-reopen-probe", daemon=True).start()
