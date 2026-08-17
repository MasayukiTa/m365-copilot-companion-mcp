"""Agents an episode can be run against.

The suite takes `agent(prompt, workdir) -> reply` and nothing else. That deliberate
narrowness is what lets the same episodes grade a simulated agent in CI, the real companion
on a workstation, and eventually a candidate harness under evaluation, without any of them
knowing about the others.

THE SSE TRAP, WRITTEN DOWN SO IT IS NOT REDISCOVERED

The bridge answers /stream as text/event-stream with Connection: keep-alive and periodic
`: ping` comment frames. A client that calls read() and waits for EOF therefore blocks
forever even though the answer arrived seconds earlier. That cost most of a day: five
"hangs" were measured at 954s, 949s, 448s, 428s and 443s against a bridge that was
answering in about 28 seconds throughout, and three separate product "fixes" were attributed
to it before the client turned out to be the fault. So BridgeAgent sends Connection: close
and stops at `event: done`.

WHY THE PROMPT CARRIES THE WORKDIR

The episode builds fixtures in a temp directory the agent has never heard of. Telling it the
absolute path is not a hint, it is the whole address space of the task -- without it the
agent works in whatever folder it last used and the grader correctly reports that nothing
happened.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import urllib.parse
import uuid

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 8765


#: The execution targets an adapter may name, and what each one can actually exercise.
#: "Covered" means a production reader on that target consults the field -- not that the
#: field exists. A target that names a field it cannot reach reintroduces the defect this
#: whole mechanism exists to prevent: two arms differing in something nothing reads.
IN_PROCESS = "in_process/v1"
IN_PROCESS_FIELDS = frozenset({
    "components.memory",              # project_memory.MEMORY_VERSIONS dispatches on it
    "parameters.memory_max_items",    # project_memory.load_notes reads it
})

#: The fleet target additionally reaches the retry and refuter budgets, because
#: run_relay_fleet defaults them from the manifest. It is declared here so the contract can
#: refuse a fleet experiment over a field even the fleet does not read.
FLEET = "relay_fleet/v1"
FLEET_FIELDS = IN_PROCESS_FIELDS | frozenset({
    "parameters.max_retries",         # -> run_relay_fleet max_transient
    "parameters.max_refute_passes",   # -> run_relay_fleet max_refute (only when refuter=True)
})


def attest_in_process(manifest):
    """What harness this process actually loaded, and the values it resolved.

    Read back through the ordinary runtime accessors rather than from the manifest that was
    handed in -- the question is what the CODE sees, and answering it from the argument would
    make the attestation a tautology.
    """
    from relay.selfimprove import manifest as M
    from relay.selfimprove import runtime_config as RC
    active = RC.active_manifest(refresh=True)
    return {
        "harness_id": M.harness_id(active),
        "execution_target": IN_PROCESS,
        "effective": {
            "memory_version": RC.component("memory"),
            "memory_max_items": RC.memory_max_items(),
        },
    }


class SimulatedAgent:
    """A scripted agent for testing the harness itself, never for measuring capability.

    Takes {episode_id: callable(workdir) -> reply}. Anything unscripted does nothing and
    says so, which grades as a failure -- the honest outcome for an agent that was not
    given an answer.
    """

    #: Runs IN THIS PROCESS, so the active manifest genuinely governs everything it touches.
    #: The claim is about the execution mechanism, not about intent -- and it is checked
    #: rather than believed, via attest() below.
    applies_manifest = True
    execution_target = IN_PROCESS
    covered_fields = IN_PROCESS_FIELDS

    def attest(self, manifest):
        return attest_in_process(manifest)

    def describe(self):
        return {"class": "SimulatedAgent", "execution_target": IN_PROCESS,
                "scripted_episodes": sorted(self.script)}

    def __init__(self, script=None, default_reply="(no action taken)"):
        self.script = dict(script or {})
        self.default_reply = default_reply
        self.calls = []

    def for_episode(self, episode_id):
        def agent(prompt, workdir):
            self.calls.append({"episode_id": episode_id, "prompt": prompt})
            fn = self.script.get(episode_id)
            if fn is None:
                return self.default_reply
            return fn(workdir) or ""
        return agent


#: Error shapes the bridge sends inside a terminated stream. Matched on the SSE payload rather
#: than on prose, so a companion answer that happens to discuss errors is not misread as one.
_BRIDGE_ERROR_KEYS = ('"error":', '"ok": false', '"ok":false')


def _bridge_error(raw: str) -> str:
    """The bridge's own error payload in this stream, or "" if there is none."""
    for line in (raw or "").splitlines():
        if not line.startswith("data: "):
            continue
        body = line[6:]
        if any(key in body for key in _BRIDGE_ERROR_KEYS):
            return body.strip()
    return ""


#: Replies that mean the SERVICE declined to work, not that the companion tried and failed.
#:
#: Three runs of the suite scored 7/22, 17/21 and 19/22, and the run that scored worst was the
#: FASTEST: median turn 50s against 73s and 77s. Fast-and-failing is the signature of a service
#: that answered without doing anything, and the observed wording is a rate-limit notice. It
#: terminates the stream properly, so `event: done` is present and the earlier classification
#: passes it straight through to the grader, which records a capability failure.
#:
#: Driving a few hundred turns through one tenant in an evening is what produces it -- so the
#: measurement causes the throttling, and the throttling then looks like the system getting
#: worse. That is the most misleading shape a benchmark defect can take.
_SERVICE_DECLINED = (
    "この量のリクエストには",
    "現在一時的に応答できません",
    "後でもう一度お試しください",
    "too many requests",
    "rate limit",
    "please try again later",
)


def service_declined(reply: str) -> str:
    """The phrase showing the service refused to serve this turn, or "".

    WHAT THIS CANNOT CATCH, and the asymmetry is worth stating where the check is rather than
    somewhere else. Throttling that arrives as a REFUSAL is detectable: the reply says so and
    it says so in a handful of words. Throttling that arrives as DEGRADATION -- a shorter
    answer, a skipped tool call, a shallower attempt under load -- looks exactly like the
    companion trying and doing worse, because that is what it is. Those turns pass this check
    and are scored as capability failures.
    
    So the classification is one-sided: it removes the loud form of an environment effect and
    leaves the quiet form in the numbers. The quiet form is also the one that biases a
    comparison, because it depends on load and load depends on how much measuring is going on.
    Pacing the measurement is the remedy; this function is not.

    Matched on the WHOLE reply being short as well as containing the phrase: a long answer
    that discusses rate limiting is an answer, and turning it into infra would delete real
    evidence in the direction that flatters the system.
    """
    text = (reply or "").strip()
    if len(text) > 200:
        return ""
    low = text.lower()
    for phrase in _SERVICE_DECLINED:
        if phrase.lower() in low:
            return phrase
    return ""


#: A bridge error can arrive as the REPLY TEXT rather than as an SSE error frame. Observed:
#: "[bridge error: RuntimeError: send failed: composer cleared without a conversation or
#: generation acknowledgement]". The frame-level check never saw it, so the grader scored a
#: stack trace as the companion's answer.
_BRIDGE_ERROR_IN_TEXT = ("[bridge error:", "[relay error:")


def _distinctive(text, minimum=4):
    """Tokens from a prompt that a reply would only contain if it had SEEN the prompt.

    Filenames, paths and long words. Short words and punctuation are shared by every sentence
    in the language and would make the check pass on anything.
    """
    import re
    out = set()
    for token in re.findall(r"[A-Za-z0-9_./\-]{%d,}" % minimum, text or ""):
        token = token.strip("./\\")
        if len(token) >= minimum and not token.isdigit():
            out.add(token.lower())
    return out


def attempted_the_task(prompt: str, reply: str) -> bool:
    """Whether this reply shows any sign of having seen the prompt.

    THE PRINCIPLED VERSION OF A PHRASE LIST. A greeting -- "hello, what can I help you with?"
    -- is what the companion says when the task never reached the tab. It settles cleanly, it
    is short, and it grades as a capability failure, which is how a delivery failure gets
    recorded as the system being bad at filesystem work.
    
    The relay already keeps a list of phrases for this and the observed greeting was not in
    it, which is the ordinary fate of a hand-written list: it fails OPEN, and the miss looks
    like a result. So this asks for positive evidence instead. Every episode prompt names a
    workdir, a filename, or a term particular to the task; a reply that shares NO distinctive
    token with its prompt and is also short has not engaged with it.

    Both conditions, because either alone is wrong. A long answer that happens to paraphrase
    without quoting is still an answer, and a short reply that names the file ("done, edited
    mod_b.py") is an attempt -- possibly a false one, but that is the grader's question and
    not this one's.
    """
    text = (reply or "").strip()
    if len(text) >= 120:
        return True
    shared = _distinctive(prompt) & _distinctive(text)
    return bool(shared)


class TurnDidNotSettle(RuntimeError):
    """The turn never completed, so there is nothing to grade.

    Distinct from a wrong answer on purpose. run_episode turns an exception into an INFRA
    result, which leaves the episode out of the denominator -- the suite's own rule that an
    environment failure must not be scored as a capability zero, applied to the one path that
    was quietly breaking it.
    """


class BridgeAgent:
    """Drives the real companion through the bridge's /stream endpoint.

    One conversation per episode by default: episodes are independent by construction, and
    letting one carry the previous one's context would make the suite's results depend on
    the order it happened to run in.

    THIS ADAPTER CANNOT RUN AN A/B, and saying so here is the point of the flag below. The
    work happens inside a bridge process that was started with whatever harness it was
    started with; this class posts a prompt and reads a reply. Setting the active manifest in
    the evaluator changes nothing about that process, so a paired comparison driven through
    here executes the SAME program on both arms and any p-value it produces is about noise.
    An independent review found this after four rounds, and it is the most damaging kind of
    defect: everything looks wired up and the number means nothing.

    Making it true requires the bridge to accept a harness for a request and honour it --
    a real change on the bridge side, not here. Until then paired_evaluate refuses this
    adapter rather than producing an unattributable result.
    """

    #: See the class docstring. Do not set this True until the bridge honours a per-request
    #: harness; the flag is what stops an invalid comparison from being reported as valid.
    applies_manifest = False

    #: EPISODES CANNOT RUN SIDE BY SIDE THROUGH THIS ADAPTER, and it is worth saying why
    #: rather than leaving the 1 to be read as caution.
    #:
    #: The bridge holds ONE Playwright page, set once at startup, driven from one page-owner
    #: thread because the sync API is thread-bound; /stream, /new and /history take a single
    #: non-blocking lock and anything that arrives while it is held is answered `busy`. So a
    #: second concurrent episode does not run second, it runs `busy` -- retrying, adding
    #: nothing but wall-clock and retry noise in the latencies.
    #:
    #: This is a property of the bridge, not of the companion, and not of the tenant. The
    #: project's fan-out path is the fleet on the other browser, and FleetAgent declares what
    #: it can actually do.
    max_concurrent_episodes = 1

    #: How long to wait between attempts when the bridge reports it is busy.
    BUSY_RETRY_S = 15

    #: Longer than the bridge's own per-turn budget. A client that gives up first turns a
    #: SLOW turn into an infra result and drops it from the denominator -- so the target's
    #: worst behaviour is the behaviour least likely to be measured, and the pass rate rises
    #: as the target gets slower.
    def __init__(self, *, host=BRIDGE_HOST, port=BRIDGE_PORT, timeout=900,
                 fresh_conversation=True, retry_busy_s=180):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.fresh_conversation = fresh_conversation
        self.retry_busy_s = retry_busy_s
        self.transcript = []
        # A MARKER PREFIX UNIQUE TO THIS ADAPTER INSTANCE, because the constant one
        # authenticated nothing. The fresh-conversation check accepts a view when every user
        # message carries the prefix -- and with a constant prefix, EVERY conversation this
        # bench has ever created qualifies. Rotating from a fresh conversation to any older
        # CompanionBench one therefore passed the check, found no marker, and reported a
        # confident non-delivery. Per-run, an old conversation is foreign, which is what it is.
        self.run_marker = "%s-%s" % (self.NONCE_PREFIX, uuid.uuid4().hex[:8])

    # -- transport ---------------------------------------------------------------------

    def _request(self, path, timeout=None):
        """One HTTP/1.0-style request that ends at `event: done` or EOF.

        Connection: close is not decoration -- see the module docstring. `event: done` is
        checked first so a keep-alive server that never closes still terminates the read.
        """
        s = socket.create_connection((self.host, self.port), timeout=15)
        s.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n"
                   % (path, self.host)).encode())
        s.settimeout(timeout or self.timeout)
        buf = b""
        started = time.time()
        try:
            while time.time() - started < (timeout or self.timeout):
                chunk = s.recv(8192)
                if not chunk:
                    break
                buf += chunk
                if b"event: done" in buf:
                    break
        except socket.timeout:
            pass
        finally:
            s.close()
        return buf.decode("utf-8", "replace")

    @staticmethod
    def _answer(raw):
        """The final answer out of an SSE body.

        `replace` frames carry the settled answer and supersede the streamed deltas; the
        deltas are only a fallback for a turn that streamed and never settled.
        """
        replace, deltas = "", []
        for line in raw.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                d = json.loads(line[6:])
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            if d.get("replace"):
                replace = d["replace"]
            elif d.get("delta"):
                deltas.append(d["delta"])
        return replace or "".join(deltas)

    #: The prefix of the per-turn marker. Short and unmistakable, so finding it in a
    #: conversation is not a judgement call.
    NONCE_PREFIX = "cb-turn"

    #: How hard to try to read the conversation back. The page lock is usually free within a
    #: few seconds of a turn ending.
    HISTORY_ATTEMPTS = 6
    HISTORY_RETRY_S = 2

    #: How the conversation is identified WITHOUT a URL, because whether the URL identifies
    #: anything depends on which page shape the target happens to be on -- and the adapter
    #: cannot know which.
    #:
    #: On the general-chat shape (`/chat/?titleId=...`) it identifies nothing: a live probe
    #: recorded in the bridge found that page.url carries no conversation id and does not
    #: change even when a sidebar click visibly switches the displayed conversation, so two
    #: different conversations compare EQUAL and a rotation check could never fire. On the
    #: agent shape (`/chat/agent/.../conversation/<guid>`) it does carry a conversation guid.
    #: An earlier version of this comment claimed the first case held everywhere; it does not,
    #: and the design does not need it to. Contents work on both shapes, a URL works on one,
    #: and asking /history to navigate to a URL can move the page away from the very
    #: conversation being inspected. So contents it is.
    #:
    #: What identifies it instead is its own contents. The conversation is fingerprinted
    #: immediately BEFORE the turn; afterwards, the same messages must still be there. If they
    #: are, the view is the one the turn was sent to, and an absent marker means the turn is
    #: genuinely not in it. If they are not, the page moved and nothing can be concluded.

    def _fingerprint(self):
        """The conversation's user messages, right now, or None if it cannot be read.

        None is not the same as an empty list. A fresh conversation legitimately has no
        messages, and confusing the two would be fatal: an empty anchor matches every
        conversation, so "I could not look" would silently become "the page did not change".
        """
        data = self._history_once()
        if not data or not data.get("ok") or data.get("truncated"):
            return None
        return [self._digest(m) for m in (data.get("messages") or [])
                if m.get("role") == "user"]

    @staticmethod
    def _digest(message) -> str:
        text = message.get("text") or message.get("content") or ""
        return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]

    def _history_once(self):
        """One /history read. The parsed body, or None if it could not be had."""
        try:
            raw = self._request("/history", timeout=90)
            body = raw.split("\r\n\r\n", 1)[-1]
            return json.loads(body[body.index("{"):])
        except Exception:
            return None

    def _confirm_delivered(self, nonce: str, anchor=None) -> dict:
        """Ask the bridge what is actually IN the conversation, and look for this turn.

        WHY THIS AND NOT THE WORKDIR. A change under the episode's workdir shows that
        something acted on that directory. It is worth having and the runner records it, but
        the adapter is handed that path too, so it cannot establish that the prompt reached
        the conversation. This can: the message is read back from the page the bridge drove,
        and the marker it carries was minted for this turn alone.

        A POSITIVE AND A NEGATIVE ARE NOT SYMMETRIC, and treating them as though they were is
        what made the first two versions of this wrong. Finding the marker proves delivery
        outright: the text is there, in a conversation, and nothing else could have put it
        there. NOT finding it proves nothing by itself -- the record may be incomplete, the
        page may have moved, the view may not have rendered yet. So a negative is returned
        only when the same conversation is demonstrably still in view AND the marker is
        demonstrably not in it. Everything else is None.

        None is not a soft failure to be tidied away. It is the honest answer to "was this
        delivered?" when the instrument cannot see, and it is reported as coverage rather
        than counted as a delivery failure. Never raises: a confirmation step that can fail
        the run it is confirming would be a measurement breaking the thing it measures.
        """
        # RETRY WHILE THE BRIDGE IS BUSY, AND WHILE THE MARKER IS SIMPLY NOT THERE YET.
        # /history needs the page lock and is asked for immediately after a turn that was
        # holding it, so the first attempt often lands on "busy" -- 4 of 10 turns in the first
        # probe, which is not a check but a coin landing on its edge. And an un-hydrated view
        # renders with no prior turns at all, so an `ok` response can simply be early; the
        # loop used to stop at the first one whatever it contained. Only the LAST look decides.
        # EVERY ATTEMPT IS RECORDED, and it has to be. This loop keeps looking until the
        # marker appears, which is optional stopping: it cannot re-send, but it can wait out
        # rendering, hydration, and an optimistically drawn bubble, and then report the
        # positive it was waiting for. The observation window is also far wider than "six
        # attempts two seconds apart" -- each /history can poll internally for ~30s and the
        # scroll pass is bounded at 45 -- so "confirmed" without a latency beside it is not a
        # number anyone can weigh.
        #
        # The per-attempt log is also what makes the old rule replayable on the SAME data.
        # The old check stopped at the first `ok` response whatever it contained; keeping
        # (ok, found, truncated) per attempt lets both rules be scored offline, which turns
        # "the old detector had a hydration race" from a mechanism I find plausible into a
        # count of turns that were absent on the first look and present on a later one --
        # with no second send in between.
        began = time.time()
        log = []
        data = None
        for attempt in range(self.HISTORY_ATTEMPTS):
            data = self._history_once()
            if data is None:
                return {"delivered": None, "why": "history unavailable",
                        "attempts": attempt + 1, "attempt_log": log,
                        "confirm_latency_s": round(time.time() - began, 1)}
            found = bool(data.get("ok")) and self._nonce_in(data, nonce)
            log.append({"ok": bool(data.get("ok")), "found": found,
                        "truncated": bool(data.get("truncated")),
                        "users": sum(1 for m in (data.get("messages") or [])
                                     if m.get("role") == "user"),
                        "at_s": round(time.time() - began, 1)})
            if found:
                return {"delivered": True, "why": "the prompt is in the conversation",
                        "conversation": (data.get("url") or "")[-80:],
                        "attempts": attempt + 1, "attempt_log": log,
                        "found_on_first_attempt": attempt == 0,
                        "saw_truncated": any(a["truncated"] for a in log),
                        "confirm_latency_s": round(time.time() - began, 1)}
            busy = "busy" in str(data.get("error") or "")
            if not data.get("ok") and not busy:
                break
            if attempt + 1 < self.HISTORY_ATTEMPTS:
                time.sleep(self.HISTORY_RETRY_S)
        trail = {"attempts": len(log) or 1, "attempt_log": log,
                 "found_on_first_attempt": False,
                 "saw_truncated": any(a["truncated"] for a in log),
                 "confirm_latency_s": round(time.time() - began, 1)}
        seen = (data or {}).get("url") or ""
        if not data or not data.get("ok"):
            return dict(trail, delivered=None,
                        why="history said: %s" % ((data or {}).get("error") or "?"))
        if data.get("truncated"):
            return dict(trail, delivered=None, conversation=seen[-80:],
                why="the conversation was captured incompletely (%s of it), and it is "
                    "scraped from the top, so the newest turn -- this one -- is the "
                    "likeliest to be missing" % (data.get("captured") or "part"))
        if anchor is None:
            return dict(trail, delivered=None, conversation=seen[-80:],
                why="the conversation could not be fingerprinted before the turn, so "
                    "there is no way to tell this view from a different one")
        users = [m for m in (data.get("messages") or []) if m.get("role") == "user"]
        if not users:
            return dict(trail, delivered=None, conversation=seen[-80:],
                why="no user messages came back after %d attempts, which is what an "
                    "un-hydrated view looks like as well as an undelivered turn"
                    % self.HISTORY_ATTEMPTS)
        after = [self._digest(m) for m in users]
        if anchor:
            if after[:len(anchor)] != anchor:
                return dict(trail, delivered=None, conversation=seen[-80:],
                    why="the messages that were there before the turn are not there "
                        "now: the page moved, and this view is not the one the turn "
                        "was sent to")
        else:
            # A FRESH CONVERSATION HAS AN EMPTY ANCHOR, AND AN EMPTY ANCHOR MATCHES ANYTHING.
            # So it is checked the other way round: everything in view must be something this
            # adapter could have put there. A foreign user message means the page is showing
            # some other conversation, whatever its URL claims.
            foreign = [m for m in users if self.run_marker not in
                       (m.get("text") or m.get("content") or "")]
            if foreign:
                return dict(trail, delivered=None, conversation=seen[-80:],
                    why="this conversation was opened fresh for the episode but holds "
                        "%d message(s) this adapter did not send, so it is not the one "
                        "the turn was sent to" % len(foreign))
        return dict(trail, delivered=False, conversation=seen[-80:],
                why="the conversation still holds the messages it had before the turn, "
                    "and this turn's prompt is not among them")

    @staticmethod
    def _nonce_in(data, nonce) -> bool:
        for message in data.get("messages") or []:
            if message.get("role") != "user":
                continue
            text = message.get("text") or message.get("content") or ""
            if nonce in text:
                return True
        return False

    def _new_conversation(self):
        deadline = time.time() + self.retry_busy_s
        while time.time() < deadline:
            raw = self._request("/new", timeout=90)
            if '"busy"' not in raw:
                return True
            time.sleep(15)
        return False

    # -- the contract ------------------------------------------------------------------

    def __call__(self, prompt, workdir):
        if self.fresh_conversation:
            # A FAILED /new IS NOT A DETAIL. The return value was discarded, so when the
            # bridge stayed busy the episode ran inside the PREVIOUS episode's conversation --
            # and one episode carrying another's context is exactly the coupling this adapter
            # opens a fresh conversation to prevent. The result would still grade, and the
            # suite's answer would depend on the order it happened to run in.
            if not self._new_conversation():
                raise TurnDidNotSettle(
                    "could not start a fresh conversation within %.0fs; running this episode "
                    "inside the previous one's context would make the suite order-dependent"
                    % self.retry_busy_s)
        full = (
            "作業フォルダは %s です。このフォルダの中だけで作業し、"
            "指示されていないファイルは変更しないでください。\n\n%s" % (workdir, prompt)
        )
        # A MARKER MINTED FOR THIS TURN, on its own line at the end, where it is inert: it
        # names nothing to do and asks for nothing to be echoed, so a companion that ignores
        # it entirely is behaving correctly. Its only job is to be findable afterwards in the
        # conversation the bridge says it used -- which is what makes delivery a fact read
        # back from the page rather than an inference from what came out of it.
        nonce = "%s-%s" % (self.run_marker, uuid.uuid4().hex[:12])
        full = "%s\n\n[%s]" % (full, nonce)
        # WHAT THIS CONVERSATION HOLDS BEFORE THE TURN, so that afterwards there is a way to
        # tell this view from a different one. Not a URL: on this target page.url carries no
        # conversation identifier at all and does not change when the displayed conversation
        # does, which is recorded in the bridge from a live probe. The contents are the only
        # identity available, so they are the identity used.
        #
        # The clock starts BEFORE this read. It costs a page-lock acquisition and it is the
        # harness's own overhead, so excluding it would report a latency the run did not have.
        started = time.time()
        anchor = self._fingerprint()
        anchor_cost = round(time.time() - started, 1)
        deadline = time.time() + self.retry_busy_s
        raw = ""
        while True:
            raw = self._request("/stream?msg=" + urllib.parse.quote(full))
            if '"busy"' not in raw[:200]:
                break
            # Sleep only if there is time left to retry INTO. The loop used to sleep 15s and
            # then re-check the deadline, so the last wait was always spent for nothing; and
            # it tested the deadline before the first request, so retry_busy_s=0 meant "never
            # ask at all" rather than "ask once and do not retry".
            if time.time() + self.BUSY_RETRY_S >= deadline:
                break
            time.sleep(self.BUSY_RETRY_S)
        reply = self._answer(raw)
        elapsed = round(time.time() - started, 1)
        settled = "event: done" in raw
        confirmed = self._confirm_delivered(nonce, anchor)
        # RECORDED, NOT ACTED ON -- and this is the second time that has had to be said.
        #
        # An undelivered turn briefly RAISED here, on the strength of a probe in which eight
        # of nine failures had no prompt in the conversation. Review took that apart and it
        # does not survive:
        #
        #   Raising skips the grader. A turn that edited the workdir CORRECTLY and then failed
        #   the history check was thrown away as infrastructure -- the harness deleting a real
        #   pass because its own instrument blinked. `_delivery_evidence` says in as many words
        #   that this belongs in the summary and not in the control flow, and this code
        #   contradicted the module it depends on.
        #
        #   It was also validated on the very failures it reclassified. Correlation between
        #   "no marker" and "failed" was read as causation and then used to re-score the same
        #   observations, with no held-out case where a DELIVERED turn was made to look absent.
        #   `test_delivery_detector_validation.py` is that held-out matrix, and the detector
        #   only earns the negative verdict on the rows where it survives it.
        #
        # So the finding stands and the response does not. The evidence goes on the record;
        # `capability` and `end_to_end` in baseline.summarise are where it is allowed to
        # change a number, because there both questions stay visible at once.
        self.transcript.append({
            # WHICH EPISODE THIS ROW BELONGS TO. The runner used to join by POSITION, which
            # is correct only while episodes run one at a time: concurrent turns all mark the
            # same index and then read each other's rows. The workdir is unique per episode
            # by construction, so it is the join that survives concurrency.
            "workdir": workdir,
            "prompt": full, "reply": reply, "elapsed_s": elapsed, "settled": settled,
            "nonce": nonce, "prompt_in_conversation": confirmed.get("delivered"),
            "anchor_cost_s": anchor_cost, "anchored": anchor is not None,
            "delivery_note": confirmed.get("why", ""),
            # THE CHECK'S OWN WORKING, carried through to the row. Without it a "confirmed"
            # cannot be told from a confirmed-after-four-looks-and-fifty-seconds, and the old
            # rule cannot be replayed on the same data to see whether it would have said no.
            "attempts": confirmed.get("attempts"),
            "found_on_first_attempt": confirmed.get("found_on_first_attempt"),
            "confirm_latency_s": confirmed.get("confirm_latency_s"),
            "saw_truncated": confirmed.get("saw_truncated"),
            "attempt_log": confirmed.get("attempt_log") or [],
            "conversation": confirmed.get("conversation", ""),
        })

        # A TURN THAT DID NOT COMPLETE IS NOT A WRONG ANSWER. `settled` was computed here and
        # then discarded, so a stream that ended without `event: done` returned whatever had
        # arrived -- usually nothing -- and the grader scored the empty reply as a capability
        # failure. Three runs of the suite scored 13/22, 6/22 and 8/22 with 19 of 22 episodes
        # changing verdict, which read as enormous model variance; the failing runs were
        # clustered at 24-25s and 46-50s and reported `produced: ""`, `read: ""`, `answer: ""`,
        # `X not created` and `calls_through_the_api: 0` -- the signature of a turn that never
        # happened rather than one that went wrong.
        #
        # The bridge always emits a terminating `done` (see _send_and_stream_once), so its
        # absence means the turn did not finish. Raising here is what the suite already does
        # with an environment failure: run_episode turns an exception into an INFRA result,
        # which is excluded from the denominator instead of counted as a zero.
        if '"busy"' in raw[:200]:
            raise TurnDidNotSettle(
                "the bridge was busy for the whole %.0fs retry window; no turn was run"
                % self.retry_busy_s)
        # THE BRIDGE EMITS `done` AFTER ITS OWN EXCEPTIONS TOO, so `settled` alone is not
        # enough: a caught bridge error arrives as a terminated stream carrying an error
        # payload, and grading that as the companion's answer is the same misclassification in
        # the other direction.
        if settled and _bridge_error(raw):
            raise TurnDidNotSettle(
                "the bridge reported an error and then terminated the stream (%s); that is "
                "the environment, not an answer" % _bridge_error(raw)[:120])
        for marker in _BRIDGE_ERROR_IN_TEXT:
            if marker in (reply or ""):
                raise TurnDidNotSettle(
                    "the bridge's own error arrived as the reply text (%s); a stack trace is "
                    "not an answer, and the frame-level check does not see this shape"
                    % (reply or "")[:120])

        # ORDERED BY HOW MUCH IS BEING INFERRED. Each of these says "this was not a valid
        # measurement", and the first three know it from something the transport reported --
        # an error payload, a missing terminator, the service saying so in its own words. The
        # last one INFERS it from the shape of the reply, which is weaker and can be wrong, so
        # it runs only when none of the others has already explained the turn.
        declined = service_declined(reply)
        if declined:
            raise TurnDidNotSettle(
                "the service declined this turn (%r): a rate-limit notice is the environment "
                "refusing to work, not the companion trying and failing. It arrives fast and "
                "terminates cleanly, which is why it was being graded as a capability result"
                % declined)
        if not settled:
            raise TurnDidNotSettle(
                "the stream ended after %.1fs without `event: done` (%d bytes, %d chars of "
                "reply): the turn did not complete, so this is an environment result and not "
                "an answer to grade" % (elapsed, len(raw), len(reply)))
        # FLAGGED, NOT RAISED. The first version of this raised, and it immediately
        # misclassified a terse correct answer -- the reply "42" to an arithmetic question
        # shares no distinctive token with its prompt and is short, which is exactly the
        # shape of the greeting it was written to catch.
        #
        # Raising would move those turns out of the denominator, which RAISES the pass rate:
        # the same direction as every other defect found here, and the one that flatters the
        # system. While the failure modes are still being discovered one at a time, the
        # honest thing is to record the suspicion where a person can look at it rather than
        # to act on it silently. `bench/companionbench/baseline.py` reports the count.
        self.transcript[-1]["delivery_suspect"] = not attempted_the_task(full, reply)
        return reply


class in_process:
    """Mark a plain callable as running in THIS process, so paired_evaluate will accept it.

        agent = in_process(lambda prompt, workdir: ...)

    Deliberately explicit: writing `in_process(...)` around a lambda that actually posts to
    another machine is a lie somebody has to type -- and one the attestation would then catch,
    because a remote executor cannot report this process's harness id.
    """

    applies_manifest = True
    execution_target = IN_PROCESS
    covered_fields = IN_PROCESS_FIELDS

    def __init__(self, fn):
        self._fn = fn

    def __call__(self, prompt, workdir):
        return self._fn(prompt, workdir)

    def attest(self, manifest):
        return attest_in_process(manifest)


def bridge_available(host=BRIDGE_HOST, port=BRIDGE_PORT) -> bool:
    """True iff something is listening. Used to SKIP live runs, never to fail them.

    A missing bridge is an environment fact. Scoring it as a capability result would be the
    same mistake the suite refuses everywhere else.
    """
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False
