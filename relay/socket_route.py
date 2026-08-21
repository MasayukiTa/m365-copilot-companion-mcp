"""Whether the fleet may talk over sockets right now, and what it does when it may not.

THE ROUTE IS A SPEED-UP, NEVER A CAPABILITY. The endpoint is undocumented and Microsoft can
close it without notice. So everything here is arranged so that losing it costs memory and
latency and nothing else: a worker that cannot use a socket opens a tab and runs the same goal
the same way. There is no path in this file that can fail a job.

THREE GUARDS, EACH FOR A DIFFERENT FAILURE

  * a circuit breaker on CONSECUTIVE failures, so one bad minute closes the route quickly
    rather than making every worker discover it separately;
  * a ONE-WAY total counter, so a route that half-works cannot flap -- keep falling back and
    it stops being offered at all, permanently for this run;
  * a token that is refreshed by OPENING A TAB, CAPTURING, AND CLOSING IT. The tab is not kept.
    That is the whole point of this work: a tab held open "just in case" was costing 1.3 GB,
    and replacing it with a tab held open for the token would have been the same mistake with
    a better excuse. Measured token life is 60-79 minutes; a capture takes about 30 seconds.

MEASURED AGAINST THE TAB PATH, 2026-08-21. The same four goals through the same fleet at
concurrency 2, changing only the route:

    peak Edge over the arm's start   sockets  +205 MB    tabs  +1653 MB
    tabs held at once                sockets     0       tabs      2
    wall clock                       sockets    77 s     tabs    104 s
    goals reaching DONE              sockets   4/4       tabs    4/4
    fallbacks                        sockets     0

The +205 MB is not the conversations -- a conversation was measured at 1.9 MB. It is the
capture tab, opened once per token lifetime and closed again, and it is counted here because
pretending a route's own overhead belongs to something else is how a measurement flatters.

STILL OFF BY DEFAULT. Four goals is not a long fleet job, and the failure this route's guards
exist for -- a consent card only a tab can click, an attachment, an endpoint withdrawn without
notice -- did not occur once in that run, so the run says nothing about them. Turning it on is
a decision to be made from a longer run's fallback rate, not from a good afternoon.
"""
from __future__ import annotations

import json
import os
import threading
import time

from relay.chathub import ChatHubError, Conversation, expires_in

#: Where routing decisions are recorded. Under .fleet/, which is gitignored -- these lines
#: carry GOAL TEXT, and goal text is the user's work, not something to publish.
DEFAULT_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           ".fleet", "socket_route.jsonl")

#: Off by default. See the module note -- this is measured on spikes, not on a day of work yet.
ENABLED = os.environ.get("MCP_FLEET_SOCKET", "").strip().lower() in ("1", "true", "yes", "on")

#: Refresh the token this long before it expires. Generous next to a 60-79 minute life: the
#: cost of being early is one cheap capture, the cost of being late is a worker falling back.
REFRESH_MARGIN_S = float(os.environ.get("MCP_FLEET_SOCKET_MARGIN_S", "600"))

#: Consecutive failures that close the route. Three is "this is not a blip".
MAX_CONSECUTIVE = int(os.environ.get("MCP_FLEET_SOCKET_MAX_CONSECUTIVE", "3"))

#: Total fallbacks tolerated across the whole run, never reset. A route that works one turn in
#: two is worse than no route: it pays the failure AND the tab open, every time.
MAX_FALLBACKS = int(os.environ.get("MCP_FLEET_SOCKET_MAX_FALLBACKS", "10"))


class SocketRoute:
    """Fleet-wide state for the socket path: is it open, and what does it hand a worker."""

    def __init__(self, *, capture_fn=None, connect_fn=None, enabled=None,
                 max_consecutive=MAX_CONSECUTIVE, max_fallbacks=MAX_FALLBACKS,
                 refresh_margin_s=REFRESH_MARGIN_S, now=time.time, log=None,
                 log_path=None):
        self.enabled = ENABLED if enabled is None else bool(enabled)
        self._capture_fn = capture_fn
        self._connect_fn = connect_fn
        self._now = now
        self._log = log or (lambda msg: None)
        #: The durable record. A printed reason is gone the moment the run ends, and the
        #: classifier that is supposed to predict which requests need a tab has to be built
        #: from what actually fell back -- so those lines outlive the process or they never
        #: become training data at all.
        #: RESOLVED HERE, NOT IN THE SIGNATURE. A default evaluated at import time cannot be
        #: redirected, and the first thing that needed redirecting was the test suite -- which
        #: was writing `route_closed` lines into the live training data and putting three
        #: events into a record that describes a route that never closed.
        self.log_path = DEFAULT_LOG if log_path is None else log_path
        self.max_consecutive = int(max_consecutive)
        self.max_fallbacks = int(max_fallbacks)
        self.refresh_margin_s = float(refresh_margin_s)

        self._lock = threading.Lock()
        #: PER AGENT, keyed by the surface it was captured from. The fleet talks to more than
        #: one: the implementation agent (T_...) and the Researcher (P_....dr_work) are
        #: different agents, and a template names its agent in the frame -- so one shared
        #: template would quietly send a research turn to the wrong agent, which answers.
        #:
        #: The token is stored per agent too. Its claims are user-scoped, so one probably
        #: serves every agent -- but "probably" is not a measurement, and the cost of not
        #: assuming is one extra 40-second capture per agent per token lifetime.
        self._entries = {}
        #: The agent a caller means when it names none. The first one captured.
        self.default_agent_url = ""
        #: Why the route is closed, or "". One-way: nothing in this file clears it.
        self.closed_reason = ""
        self.consecutive = 0
        #: Never decremented. The flap guard.
        self.fallbacks = 0
        self.turns = 0

    # ---- the durable record ------------------------------------------------------------------

    def record(self, event: str, **fields) -> None:
        """Append one line about a routing decision. Best effort; never raises, never blocks.

        WRITTEN ONLY WHEN THE ROUTE IS ENABLED. With the flag off there is no routing decision
        to record, and a fleet running the way it always has should not start writing a file
        it never wrote before.
        """
        if not self.enabled or not self.log_path:
            return
        try:
            now = float(self._now())
            rec = {"ts": now,
                   "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
                   "event": str(event)}
            rec.update(fields)
            d = os.path.dirname(self.log_path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + chr(10))
                fh.flush()
        except Exception:
            # A record that cannot be written must not cost a turn. The route's job is to be
            # cheaper than a tab, not to be a logging subsystem.
            pass

    # ---- state ------------------------------------------------------------------------------

    def open(self) -> bool:
        """Whether a worker may be offered a socket. Cheap; called per admission."""
        return bool(self.enabled and not self.closed_reason)

    def close_route(self, reason: str) -> None:
        """Stop offering sockets for the rest of this run. There is no reopen, by design."""
        if self.closed_reason:
            return
        self.closed_reason = reason
        self._log("[socket_route] closed: %s -- workers now open tabs" % reason)
        self.record("route_closed", reason=reason, turns=self.turns,
                    fallbacks=self.fallbacks)

    def note_failure(self, reason: str) -> None:
        """A worker fell back. Counts twice: against the blip guard and the flap guard."""
        with self._lock:
            self.consecutive += 1
            self.fallbacks += 1
            consecutive, fallbacks = self.consecutive, self.fallbacks
        self._log("[socket_route] fallback %d (consecutive %d): %s"
                  % (fallbacks, consecutive, reason))
        if consecutive >= self.max_consecutive:
            self.close_route("%d consecutive failures, last: %s" % (consecutive, reason))
        elif fallbacks >= self.max_fallbacks:
            self.close_route("%d fallbacks this run: the route is not reliable enough to be "
                             "worth the failed turn plus the tab open" % fallbacks)

    def note_success(self) -> None:
        with self._lock:
            self.consecutive = 0
            self.turns += 1

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "open": self.open(),
            "closed_reason": self.closed_reason,
            "turns": self.turns,
            "fallbacks": self.fallbacks,
            "consecutive": self.consecutive,
            "agents": len(self._entries),
            "token_seconds_left": int(self.token_life()),
        }

    # ---- the token and the shape --------------------------------------------------------------

    def _key(self, agent_url=None) -> str:
        return str(agent_url or self.default_agent_url or "")

    def token_life(self, agent_url=None) -> float:
        entry = self._entries.get(self._key(agent_url)) or {}
        tok = entry.get("token") or ""
        return expires_in(tok, now=self._now()) if tok else 0.0

    def template_for(self, agent_url=None):
        return (self._entries.get(self._key(agent_url)) or {}).get("template")

    def needs_refresh(self, agent_url=None) -> bool:
        if self.template_for(agent_url) is None:
            return True
        return self.token_life(agent_url) <= self.refresh_margin_s

    def refresh(self, context, agent_url=None) -> bool:
        """Open a tab, capture the token and the request shape, CLOSE THE TAB.

        Returns False rather than raising: a capture that fails means workers open tabs, which
        is exactly what they did before this route existed.
        """
        if not self.open() or self._capture_fn is None:
            return False
        key = self._key(agent_url)
        if not self.needs_refresh(key):
            return True
        try:
            token, template = self._capture_fn(context, agent_url)
        except Exception as exc:
            self.note_failure("capture failed for %s: %s: %s"
                              % (key[:40] or "(default)", type(exc).__name__, str(exc)[:140]))
            return False
        with self._lock:
            self._entries[key] = {"token": token, "template": template}
            if not self.default_agent_url:
                self.default_agent_url = key
        self._log("[socket_route] captured: %.0f min of token, agent %s"
                  % (self.token_life(key) / 60.0, (template.gpt_id or "(none)")[:28]))
        return True

    def driver_for(self, name: str, agent_url=None, model: str = "",
                   turn_timeout_s: float = 600.0, frame_timeout_s: float = 90.0):
        """A socket driver for one worker or side agent, or None to open a tab instead.

        `agent_url` selects WHICH agent -- omitted means the first one captured, which is the
        fleet's implementation agent. `model` names the deep-research model when the template
        carries that field; it is applied to a copy, so the shared template is never edited.

        The timeouts are arguments because a research turn is not a chat turn: ten minutes of
        thinking is normal for one and a hang for the other.
        """
        if not self.open() or self._connect_fn is None:
            return None
        key = self._key(agent_url)
        with self._lock:
            entry = dict(self._entries.get(key) or {})
        template, token = entry.get("template"), entry.get("token") or ""
        if not template or expires_in(token, now=self._now()) <= 0:
            return None
        if model:
            template = template.with_deep_research_model(model)
        from relay.socket_driver import CopilotSocketDriver

        # A TOKEN SUPPLIER, NOT A TOKEN. The conversation asks whenever it needs one, so a
        # refresh that happens mid-goal reaches a conversation that is already running.
        def supply():
            with self._lock:
                return (self._entries.get(key) or {}).get("token") or ""

        conv = Conversation(supply, template=template,
                            turn_timeout_s=float(turn_timeout_s),
                            frame_timeout_s=float(frame_timeout_s))
        return CopilotSocketDriver(conv, connect=self._connect_fn)


def websocket_connect(url, headers, timeout_s):
    """The real socket, wrapped to the three methods a Conversation uses.

    Kept out of chathub.py deliberately: that module chooses no socket library, which is what
    lets its protocol be tested without a network.

    NOT CALLABLE FROM A THREAD PLAYWRIGHT'S SYNC API IS DRIVING. That API owns an event loop on
    its thread and this starts one of its own, so inside a `with sync_playwright()` block it
    raises "Cannot run the event loop while another loop is running". Production never hits it
    -- CopilotSocketDriver runs every turn on its own thread -- but a script that captures a
    token and then speaks in the same block will, and the error does not say why.
    """
    import asyncio

    import websockets

    class _WS:
        def __init__(self):
            self._loop = asyncio.new_event_loop()
            self._ws = self._loop.run_until_complete(asyncio.wait_for(
                websockets.connect(url, additional_headers=headers or None,
                                   max_size=8 * 1024 * 1024, open_timeout=timeout_s),
                timeout=timeout_s + 5))

        def send(self, blob):
            self._loop.run_until_complete(self._ws.send(blob))

        def recv(self, per_frame_timeout_s):
            try:
                out = self._loop.run_until_complete(
                    asyncio.wait_for(self._ws.recv(), timeout=per_frame_timeout_s))
            except asyncio.TimeoutError:
                return None
            return out.decode("utf-8", "replace") if isinstance(out, bytes) else out

        def close(self):
            try:
                self._loop.run_until_complete(self._ws.close())
            finally:
                self._loop.close()

    try:
        return _WS()
    except Exception as exc:
        raise ChatHubError("could not open the socket: %s: %s"
                           % (type(exc).__name__, str(exc)[:160]))


def capture_via_tab(context, agent_url):
    """Open a tab on the agent surface, capture, and close it. The tab is NOT kept."""
    from relay.chathub_capture import capture
    from relay.relay_fleet import _open_fresh

    page = _open_fresh(context, agent_url)
    try:
        return capture(page)
    finally:
        try:
            page.close()
        except Exception:
            pass
