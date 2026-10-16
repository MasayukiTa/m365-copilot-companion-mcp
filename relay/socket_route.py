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

ON BY DEFAULT SINCE 2026-08-21, and the thing that justified flipping it was not the speed or
the memory. It was a FAILURE. A socket review ran 400 seconds, exceeded its deadline, fell back
to a page and finished with a correct verdict; the route recorded the reason and the job was
never at risk. Until that happened the recovery path was a claim, and a route that has never
been seen to fail safely should not be anyone's default however well it performs.

What the same day measured, for the record:

    chat turns        20+ across 8 request classes, zero fallbacks, Work IQ included --
                      inbox, calendar read, calendar WRITE, SharePoint, verified against
                      ground truth rather than against how convincing the answer sounded
    memory            +205 MB peak against +1,653 for the same four goals on tabs
    speed             77s against 104s for those goals; a deep research 198-255s against
                      673-809 in a tab
    side agents       ran at all. ram_room_for_tab() was False on this box, so research and
                      review would have been SKIPPED rather than merely slowed

Set MCP_FLEET_SOCKET=0 to go back to tabs everywhere.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

from relay.chathub import ChatHubError, Conversation, expires_in

#: Where routing decisions are recorded. Under .fleet/, which is gitignored -- these lines
#: carry GOAL TEXT, and goal text is the user's work, not something to publish.
DEFAULT_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           ".fleet", "socket_route.jsonl")

#: ON by default since 2026-08-21. See the module note for what was measured before flipping
#: it, and in particular for the fallback that had to be observed first: a route whose recovery
#: has never fired is a route whose recovery is a claim.
#:
#: UNSET MEANS ON; AN EXPLICIT BLANK MEANS OFF. The difference matters because scripts disable
#: this by assigning an empty string, and `get(key, "1")` cannot tell that apart from absence.
_RAW = os.environ.get("MCP_FLEET_SOCKET")
ENABLED = (True if _RAW is None
           else _RAW.strip().lower() not in ("0", "false", "no", "off", ""))

#: Refresh the token this long before it expires. Generous next to a 60-79 minute life: the
#: cost of being early is one cheap capture, the cost of being late is a worker falling back.
REFRESH_MARGIN_S = float(os.environ.get("MCP_FLEET_SOCKET_MARGIN_S", "600"))

#: Consecutive failures that close the route. Three is "this is not a blip".
MAX_CONSECUTIVE = int(os.environ.get("MCP_FLEET_SOCKET_MAX_CONSECUTIVE", "3"))

#: Total fallbacks tolerated across the whole run, never reset. A route that works one turn in
#: two is worse than no route: it pays the failure AND the tab open, every time.
MAX_FALLBACKS = int(os.environ.get("MCP_FLEET_SOCKET_MAX_FALLBACKS", "10"))

#: Stalled conversations only close the route if NO conversation made progress for this long.
STALL_WINDOW_S = float(os.environ.get("MCP_FLEET_SOCKET_STALL_WINDOW_S", "120"))


class SocketRoute:
    """Fleet-wide state for the socket path: is it open, and what does it hand a worker."""

    def __init__(self, *, capture_fn=None, connect_fn=None, enabled=None,
                 max_consecutive=MAX_CONSECUTIVE, max_fallbacks=MAX_FALLBACKS,
                 refresh_margin_s=REFRESH_MARGIN_S, now=time.time, log=None,
                 log_path=None, stall_window_s=STALL_WINDOW_S):
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
        #: MCP session -> worker/conversation bindings (relay/mcp_attribution.py), rebuilt from
        #: the log after a restart, and the tool ledger the in-flight view reads (None = the
        #: repo's tool_events.jsonl, resolved at use).
        from relay import mcp_attribution as _attr
        self.mcp_bindings = _attr.Registry(lambda: self.log_path)
        self.ledger_path = None
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
        #: Monotonic per-agent capture generation. Async helpers remember the generation they
        #: started from and may install only if nobody else installed a newer capture meanwhile.
        self._capture_revision = {}
        #: ONE CAPTURE AT A TIME, PER AGENT. `refresh` reads needs_refresh() and then captures,
        #: and nothing stopped two workers passing that check together -- measured on a run of
        #: 24 goals at concurrency 6, where the tab count peaked at 3 instead of 2. Two capture
        #: tabs is twice the transient memory and two real turns to learn one token.
        self._capturing = {}
        #: The agent a caller means when it names none. The first one captured.
        self.default_agent_url = ""
        #: Why the route is closed, or "". One-way: nothing in this file clears it.
        self.closed_reason = ""
        self.consecutive = 0
        #: Never decremented. The flap guard.
        self.fallbacks = 0
        self.turns = 0
        #: When any conversation last made progress or completed a turn. Starts at construction
        #: so "silent for N s" is measured from when the route began to be offered.
        self._last_progress_ts = float(self._now())
        self.stall_window_s = float(stall_window_s)
        self._recent_failures = []
        self._recent_stalls = []

    # ---- the durable record ------------------------------------------------------------------

    def conversation_for_goal(self, goal: str) -> str:
        """The conversation id of the most recent socket worker that ran `goal`, or "".

        Reads the same append-only log `record` writes, which is the only place this was ever
        kept. Exists so a follow-up can carry prior context: the server holds the history
        keyed by this id -- measured, with a control -- so handing it back is the whole of
        what a resume needs.

        Matched on the goal text because that is what identifies a worker here: it is the
        transcript key and the replay envelope already. Newest wins, so re-running a goal
        points a later follow-up at the later conversation.
        """
        want = (goal or "").strip()[:600]
        if not want:
            return ""
        found = ""
        try:
            with open(self.log_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue          # several processes append; a torn tail is normal
                    if not isinstance(rec, dict):
                        continue
                    if rec.get("event") != "worker_done":
                        continue
                    if (rec.get("goal") or "").strip() != want:
                        continue
                    cid = str(rec.get("conv_client") or "").strip()
                    if cid:
                        found = cid
        except FileNotFoundError:
            return ""
        except Exception:
            return ""
        return found

    def record_mcp_bound(self, **fields) -> None:
        """A conversation's MCP session was identified: write `mcp_session_bound`
        {mcp_session, worker, conversation, turn_at_bind, how, candidates, ...} and remember it,
        so the tool ledger can be read per worker (relay/mcp_attribution.py). Never raises."""
        try:
            rec = dict(fields)
            rec["ts"] = float(self._now())
            self.mcp_bindings.add(rec)
            self.record("mcp_session_bound", **fields)
        except Exception:
            pass

    def record_turn_stats(self, s: dict) -> None:
        """`socket_turn_stats` for a finished (or failed) turn, then `socket_turn_tools`: the MCP
        tool calls of this worker's conversation that were in flight during the turn's window.
        The second record is what tells a stall that is waiting on a long server-side call from
        one that is waiting on nothing. Never raises."""
        self.record("socket_turn_stats", **s)
        try:
            from relay import mcp_attribution as attr
            t0, t1 = s.get("t0"), float(self._now())
            if not t0:
                return
            path = self.ledger_path
            if path is None:
                from tools import tool_ledger
                path = str(tool_ledger._repo_path())
            fields = attr.turn_tools_for(s.get("worker", ""), s.get("turn"), s.get("conversation", ""),
                                         float(t0), t1, path, self.mcp_bindings.snapshot())
            if fields is not None:
                fields["outcome"] = s.get("outcome", "")
                self.record(attr.TURN_TOOLS_EVENT, **fields)
        except Exception:
            pass

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
        """Stop offering sockets for the rest of this run. There is no reopen, by design.

        Serialize the close decision with capture installation. Otherwise a helper can pass an
        ``open()`` check just before this method closes the route and then publish credentials
        into a route that policy already declared unusable.
        """
        with self._lock:
            if self.closed_reason:
                return
            self.closed_reason = reason
            turns, fallbacks = self.turns, self.fallbacks
        self._log("[socket_route] closed: %s -- goals wait (no tab is ever opened); the route is "
                  "probed for reopening without spending a turn" % reason)
        self.record("route_closed", reason=reason, turns=turns, fallbacks=fallbacks)

    @staticmethod
    def _worker_of(reason: str) -> str:
        m = re.match(r"\s*([A-Za-z]\w{0,15}):\s", reason or "")
        return m.group(1) if m else ""

    @staticmethod
    def _frames_of(reason: str) -> str:
        m = re.search(r"\[frames: ([^\]]*)\]", reason or "")
        return m.group(1) if m else ""

    def note_progress(self, worker: str = "") -> None:
        """Any conversation made progress. A progressing conversation proves the route is healthy,
        so the run of consecutive failures is broken: consecutive means consecutive across ALL
        conversations in time order."""
        with self._lock:
            self._last_progress_ts = float(self._now())
            self.consecutive = 0
            self._recent_failures = []
            self._recent_stalls = []

    def _trip_record(self, kind: str, reason: str) -> None:
        with self._lock:
            fails = list(self._recent_failures)
            stalls = list(self._recent_stalls)
        per_worker = {}
        frames = {}
        for f in fails + stalls:
            per_worker[f["worker"] or "?"] = per_worker.get(f["worker"] or "?", 0) + 1
            for part in str(f.get("frames") or "").split(", "):
                m = re.match(r"(.+) x(\d+)$", part)
                if m:
                    frames[m.group(1)] = frames.get(m.group(1), 0) + int(m.group(2))
        self.record("route_breaker_trip", kind=kind, reason=reason[:300],
                    connection_failures=len(fails), stalls=len(stalls),
                    per_worker=per_worker, frames=frames,
                    consecutive=self.consecutive, fallbacks=self.fallbacks)

    def note_stall(self, worker: str, reason: str, frames: str = "") -> bool:
        """One conversation stalled. Recorded, NOT a connection failure: the worker recycles that
        conversation by itself. It closes the route only if no conversation anywhere made
        progress for ``stall_window_s`` and ``max_consecutive`` distinct conversations stalled
        in that silence (then nothing is working, which is a route fault)."""
        frames = frames or self._frames_of(reason)
        now = float(self._now())
        with self._lock:
            self._recent_stalls.append({"worker": worker, "reason": reason[:200],
                                        "frames": frames, "ts": now})
            silent_for = now - self._last_progress_ts
            workers = {s["worker"] for s in self._recent_stalls}
            nstalls = len(workers)
        self.record("conversation_stall", worker=worker, reason=reason[:300], frames=frames,
                    silent_for_s=round(silent_for, 1), stalled_workers=nstalls)
        self._log("[socket_route] conversation stall (%s), recycled per worker; nothing "
                  "progressing for %.0fs, %d stalled" % (worker, silent_for, nstalls))
        if nstalls >= self.max_consecutive and silent_for >= self.stall_window_s:
            why = ("%d conversations stalled and none progressed anywhere for %.0fs, last: %s"
                   % (nstalls, silent_for, reason))
            self._trip_record("stalls_with_no_progress", why)
            self.close_route(why)
            return True
        return False

    def note_failure(self, reason: str) -> None:
        """A worker fell back. Only failures that indict the CONNECTION or TOKEN count toward
        the breaker; a per-conversation stall goes to note_stall and anything else is recorded
        and left to the worker."""
        from relay.transport_policy import indicts_connection, is_stall
        worker = self._worker_of(reason)
        if is_stall(reason):
            self.note_stall(worker, reason)
            return
        if not indicts_connection(reason):
            self.record("failure_not_counted", worker=worker, reason=reason[:300])
            self._log("[socket_route] fallback not counted (does not indict the connection): %s"
                      % reason)
            return
        with self._lock:
            self.consecutive += 1
            self.fallbacks += 1
            consecutive, fallbacks = self.consecutive, self.fallbacks
            self._recent_failures.append({"worker": worker, "reason": reason[:200],
                                          "frames": self._frames_of(reason)})
        self._log("[socket_route] fallback %d (consecutive %d): %s"
                  % (fallbacks, consecutive, reason))
        if consecutive >= self.max_consecutive:
            why = "%d consecutive failures, last: %s" % (consecutive, reason)
            self._trip_record("connection", why)
            self.close_route(why)
        elif fallbacks >= self.max_fallbacks:
            why = ("%d fallbacks this run: the route is not reliable enough to be "
                   "worth the failed turn plus the tab open" % fallbacks)
            self._trip_record("fallbacks", why)
            self.close_route(why)

    def note_capture_failure(self, reason: str) -> None:
        """A TOKEN CAPTURE failed. Logged and recorded, but NOT counted against the breaker.

        The breaker exists because a failed route costs a failed turn plus a tab. There is no tab
        any more (relay/no_tab.py): goals simply wait for a token, and the capture is retried with
        backoff (relay/token_provider.CapturePolicy). Closing the route over a failed capture would
        only turn "waiting for token" into a shut route that cannot even be probed -- it has no
        token to probe with."""
        self._log("[socket_route] capture failed (goals keep waiting): %s" % reason)
        self.record("capture_failed", reason=reason[:300])

    def note_success(self) -> None:
        with self._lock:
            self.consecutive = 0
            self.turns += 1
            self._last_progress_ts = float(self._now())
            self._recent_failures = []
            self._recent_stalls = []

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "open": self.open(),
            "closed_reason": self.closed_reason,
            "turns": self.turns,
            "fallbacks": self.fallbacks,
            "consecutive": self.consecutive,
            "last_progress_age_s": int(max(0.0, self._now() - self._last_progress_ts)),
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

    def capture_revision(self, agent_url=None) -> int:
        """Current per-agent capture generation, for optimistic async installation."""
        key = self._key(agent_url)
        with self._lock:
            return int(self._capture_revision.get(key) or 0)

    def template_for(self, agent_url=None):
        return (self._entries.get(self._key(agent_url)) or {}).get("template")

    def ready(self, agent_url=None) -> bool:
        """Whether a worker can take a socket *right now* without any browser capture.

        ``open()`` only says the route has not tripped its breaker. Before async refresh existed
        admission could treat open==socket because it synchronously captured immediately before
        attach. Once capture is off the sweep, readiness must also require a live token/template
        or tab accounting underestimates every fallback.
        """
        if not self.open():
            return False
        key = self._key(agent_url)
        with self._lock:
            entry = dict(self._entries.get(key) or {})
        token = entry.get("token") or ""
        return bool(entry.get("template") is not None and token and
                    expires_in(token, now=self._now()) > 0)

    def install_capture(self, token, template, agent_url=None, expected_revision=None) -> bool:
        """Install plain capture data produced elsewhere (for example a helper process).

        ``expected_revision`` is the generation observed when an async capture started. If a
        synchronous caller (Research/Refuter/etc.) installs something newer while that helper is
        away, the late helper must not roll the route back to older credentials/template data.
        No Playwright object crosses this boundary.
        """
        if not token or template is None:
            return False
        key = self._key(agent_url)
        with self._lock:
            if not self.enabled or self.closed_reason:
                return False
            current = int(self._capture_revision.get(key) or 0)
            if expected_revision is not None and current != int(expected_revision):
                return False
            self._entries[key] = {"token": token, "template": template}
            self._capture_revision[key] = current + 1
            if not self.default_agent_url:
                self.default_agent_url = key
        self._log("[socket_route] captured: %.0f min of token, agent %s"
                  % (self.token_life(key) / 60.0, (template.gpt_id or "(none)")[:28]))
        return True

    def token_for(self, agent_url=None) -> str:
        """The bearer this route holds, for a caller that must speak HTTP alongside it.

        driver_for() hands the CONVERSATION a supplier rather than a token, because a
        refresh mid-goal has to reach a conversation that is already running. An
        attachment upload is not like that: it is one request, made once, before the turn
        it belongs to -- so it takes the value, and a stale one simply gets an HTTP
        refusal that relay/socket_attachment turns into a tab.
        """
        return (self._entries.get(self._key(agent_url)) or {}).get("token") or ""

    def needs_refresh(self, agent_url=None) -> bool:
        if self.template_for(agent_url) is None:
            return True
        return self.token_life(agent_url) <= self.refresh_margin_s

    def refresh(self, context, agent_url=None) -> bool:
        """Open a tab, capture the token and the request shape, CLOSE THE TAB.

        Returns False rather than raising: a capture that fails means goals keep waiting for a
        token (there is no tab fallback) and the capture is retried with backoff.
        """
        if not self.open() or self._capture_fn is None:
            return False
        key = self._key(agent_url)
        if not self.needs_refresh(key):
            return True
        with self._lock:
            gate = self._capturing.get(key)
            if gate is None:
                gate = self._capturing[key] = threading.Lock()
        # The SECOND caller waits, and then finds the token the first one stored -- so it
        # returns without opening a tab of its own rather than racing for the same answer.
        with gate:
            if not self.needs_refresh(key):
                return True
            return self._refresh_locked(context, agent_url, key)

    def _refresh_locked(self, context, agent_url, key) -> bool:
        # A SYNCHRONOUS CAPTURE HOLDS THE CALLER'S THREAD (the fleet sweep, for the research and
        # refuter side agents) for as long as the page takes, and the stall watchdog reads that
        # silence as a wedged Edge. Declared here -- the one place every synchronous capture
        # passes through -- so no caller has to remember to.
        from relay import blocking_calls
        try:
            with blocking_calls.blocking("token_capture", blocking_calls.SYNC_CAPTURE_CEILING_S):
                token, template = self._capture_fn(context, agent_url)
        except Exception as exc:
            self.note_capture_failure("capture failed for %s: %s: %s"
                              % (key[:40] or "(default)", type(exc).__name__, str(exc)[:140]))
            return False
        return self.install_capture(token, template, key)

    def driver_for(self, name: str, agent_url=None, model: str = "",
                   turn_timeout_s: float = 600.0, frame_timeout_s: float = 90.0,
                   conversation_id: str = ""):
        """A socket driver for one worker or side agent, or None to open a tab instead.

        `conversation_id` continues an earlier conversation instead of starting one. MEASURED,
        not assumed, on 2026-08-24: a passphrase planted in one process was recovered verbatim
        from a second process that had only this id -- across a fresh token, a fresh session
        id, and either value of isStartOfSession. The control arm, an unused id, answered
        "I do not know", so the experiment could fail and did not.

        Two things that measurement settled and that would otherwise have been guessed. The
        backend accepted the id the client proposed and answered with the same one, so there
        is no second identity to reconcile. And the session id carries nothing: it may be
        fresh every time, which is what the one-key-per-connection rule already required.

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
        if conversation_id:
            # The one field that carries the history. Set here rather than in Conversation's
            # constructor so that "start a conversation" stays the default and continuing one
            # is something a caller has to ask for by name.
            conv.conversation_id = str(conversation_id)
            # AND THE FRAME MUST NOT CLAIM OTHERWISE. `started` defaults to turns == 0, so a
            # rehydrated conversation announced isStartOfSession on the very turn that was
            # continuing an existing one. Measured harmless today, and the docstring above
            # said the frame would stay truthful anyway -- it was not. A protocol that
            # refuses frames whose fields disagree is one bad assumption away from refusing
            # this, and the assumption costs nothing to remove.
            conv.turns = max(1, int(getattr(conv, "turns", 0) or 0))
        drv = CopilotSocketDriver(conv, connect=self._connect_fn)
        drv.worker_name = str(name or "")
        drv.stats_sink = self.record_turn_stats
        return drv


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
    """REMOVED: this ran one REAL Copilot turn in a tab to read the token and the request
    template. Copilot never runs in a browser tab and a token capture never sends a turn (owner
    rule, 2026-10-14). The only capture is relay.profile_token.capture_via_profile, which reads
    the token from a page's own request headers and the template from the on-disk cache."""
    from relay.no_tab import forbid_copilot_tab
    forbid_copilot_tab("token capture with a real turn (capture_via_tab)")
