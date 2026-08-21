"""Talk to the Copilot backend over its own WebSocket, so N conversations stop costing N SPAs.

WHY THIS EXISTS, AND WHAT IT IS NOT

It is a memory measure. Measured on this machine 2026-08-20: a FRESH Copilot tab holds
137-161 MB of JS heap with 926 DOM nodes and 2 KB of visible text, and renderers sit at
285-657 MB; an unbounded conversation had grown one to 1,340 MB. Roughly 300 MB of that is the
web application itself, multiplied by every tab. A socket carries the same conversation
without rendering it, which is the only way to stop multiplying that floor.

IT DOES NOT AUTHENTICATE, AND CANNOT. `token_supplier` is a callable the caller provides, and
there is no code path here that reaches an identity provider. That is deliberate and is the
line this module is built around:

    the browser signs in; this module only speaks.

The reason is specific. The public tool that pioneered this route obtains its token by naming
Microsoft's own first-party client id and using the family-refresh-token behaviour those
clients share -- documented since 2022 as an abuse technique, with defensive tooling built to
spot it. That is impersonation of a client, it routes around a tenant's app-consent
governance, and it is not something this repository will do. Reusing a token the signed-in
browser already holds for the same user and the same backend is a different act. It is not a
clean one either -- the endpoint below is undocumented and can change without notice -- but
the difference between "reuse what was issued" and "mint what was not" is the whole point.

`test_chathub.py` fails if any identity-provider host appears in this file.

ORIGIN IS NOT SENT BY DEFAULT

The reference implementation sets Origin and a browser User-Agent. Whether the server REQUIRES
them is unknown and worth knowing: if it does not, this path involves no client mimicry at all;
if it does, connecting means presenting as a browser, which is a smaller thing than presenting
as a different application but is not nothing. So the default sends neither, the caller must
opt in, and the answer is a measurement rather than an assumption.

FALLBACK IS THE CALLER'S JOB, and the fleet already has the shape for it: a worker opens a tab
on attach and frees it on release, so "try the socket, fall back to a tab" rides an existing
lifecycle. If Microsoft closes this route, the fleet loses a speed-up rather than a capability.
"""
from __future__ import annotations

import base64
import json
import os
import time
import uuid

#: SignalR frames are terminated by this, not by newlines.
RS = "\x1e"

WS_BASE = "wss://substrate.office.com/m365Copilot/Chathub"

#: Feature flags the browser client sends. Carried because the backend's behaviour depends on
#: them, NOT because they identify anything -- they select response shape (streaming mode,
#: citation handling, message splitting). Kept in one place so a drift in behaviour has one
#: obvious suspect.
DEFAULT_VARIANTS = ",".join((
    "feature.IsStreamingModeInChatRequestEnabled",
    "IncludeSourceAttributionsConcise",
    "SkipPublishEmptyMessage",
    "feature.EnableReferencesListCompleteSignal",
    "feature.StorageMessageSplitDisabled",
))

BROWSER_ORIGIN = "https://m365.cloud.microsoft"


class ChatHubError(RuntimeError):
    """The socket route did not work. Callers fall back to a tab rather than fail a goal."""


def claims(access_token: str) -> dict:
    """The JWT payload, unverified, for the two fields the URL needs.

    UNVERIFIED ON PURPOSE and safe here: the signature protects the SERVER against a forged
    token, and nothing this module decides is a security decision. It reads `oid` and `tid` to
    build a URL, and a token that lies about them simply fails to connect.
    """
    try:
        body = access_token.split(".")[1]
        body += "=" * (-len(body) % 4)
        return json.loads(base64.urlsafe_b64decode(body).decode("utf-8"))
    except Exception as exc:
        raise ChatHubError("could not read the token's claims: %s" % type(exc).__name__)


def expires_in(access_token: str, now=None) -> float:
    """Seconds until this token expires, or 0.0 when it cannot be read.

    Callers refresh by ASKING THE BROWSER AGAIN, never by minting. A token this module cannot
    read is treated as already expired, so the fallback runs rather than a doomed connection.
    """
    try:
        exp = float(claims(access_token).get("exp") or 0)
    except ChatHubError:
        return 0.0
    return max(0.0, exp - float(now if now is not None else time.time()))


class RequestTemplate:
    """What the signed-in client sends, minus the values that belong to one particular turn.

    WHY A TEMPLATE AND NOT CONSTANTS. The request carries `variants`, which on 2026-08-20 held
    68 feature flags -- among them `Agt_bizchat_enableGpt5ForHelix`, which selects the
    responding model. A shortened list is not a smaller request, it is a different product, and
    a list frozen into this file would drift away from what the tenant is actually being served
    the first time Microsoft ships a flag. So the browser tab that already signs in is also the
    source of the request shape, and this class holds one capture of it.

    It also removes an entire class of mistake. Everything here was captured, so nothing here
    was guessed -- and every field this module got wrong today (`agent=web` instead of `Agent`,
    a missing `gptId`, `scenario`, a `conversationId` the client puts in the URL rather than the
    frame, `tone` in the wrong case) was a value someone had composed rather than observed.

    A template NEVER holds a credential: `access_token` is stripped on the way in, because a
    template is passed around and logged and a token must not be.
    """

    #: Query keys that name one connection or one turn. Taken from the caller every time, never
    #: from the template -- a stale one describes somebody else's turn.
    VOLATILE_QUERY = ("access_token", "ConversationId", "X-SessionId",
                      "XRoutingParameterSessionKey", "chatsessionid", "clientrequestid")

    def __init__(self, query, frame):
        self.query = {k: v for k, v in dict(query or {}).items() if k != "access_token"}
        self.frame = json.loads(json.dumps(frame or {}))

    @classmethod
    def from_capture(cls, socket_url: str, chat_frame: dict) -> "RequestTemplate":
        """From one observed connection: its URL and the chat frame sent over it."""
        from urllib.parse import urlsplit, parse_qsl

        return cls(dict(parse_qsl(urlsplit(socket_url or "").query)), chat_frame)

    @property
    def gpt_id(self) -> str:
        """The agent this template talks to, or "" for the default Copilot."""
        return ((self.frame.get("threadLevelGptId") or {}).get("id") or "")

    def frame_for(self, text, *, session_id, request_id, started):
        """The captured frame with this turn's values written into it, and nothing else."""
        f = json.loads(json.dumps(self.frame))
        f["sessionId"] = session_id
        f["clientCorrelationId"] = request_id
        f["traceId"] = request_id
        f["isStartOfSession"] = bool(started)
        # THE CLIENT DOES NOT SEND THIS IN THE FRAME -- it is a URL parameter. We did, and a
        # frame that names a different conversation than its own connection is one of the
        # things a server is entitled to call InvalidRequest.
        f.pop("conversationId", None)
        if isinstance(f.get("clientInfo"), dict):
            f["clientInfo"]["clientSessionId"] = session_id
        msg = f.setdefault("message", {})
        msg["text"] = text
        msg["requestId"] = request_id
        if isinstance(msg.get("clientInfo"), dict):
            msg["clientInfo"]["clientSessionId"] = session_id
        return f


def build_ws_url(access_token: str, *, session_id: str, conversation_id: str,
                 request_id: str, variants: str = DEFAULT_VARIANTS,
                 template: "RequestTemplate" = None, session_key: str = None) -> str:
    """The connection URL. The token travels in the query string -- the server's choice.

    WITHOUT A TEMPLATE this builds the shape taken from the public gateway, which was MEASURED
    REJECTED on 2026-08-20: the server read it and answered `result.value = "InvalidRequest"`.
    It is kept because it is what the protocol tests exercise, and because the difference
    between it and a capture is the finding. Production passes a template.

    `session_key` is the connection's key. The client sends ONE value in `chatsessionid`,
    `clientrequestid` and `XRoutingParameterSessionKey`; we sent three different per-turn ids.
    """
    from urllib.parse import urlencode

    c = claims(access_token)
    oid, tid = c.get("oid"), c.get("tid")
    if not oid or not tid:
        raise ChatHubError("the token carries no oid/tid; it is not a token for this backend")
    if template is not None:
        q = dict(template.query)
    else:
        q = {
            "variants": variants,
            "source": '"officeweb"',
            "product": "Office",
            "agentHost": "Bizchat.FullScreen",
            "agent": "web",
            "scenario": "OfficeWebIncludedCopilot",
        }
    key = session_key or request_id
    q.update({
        "chatsessionid": key,
        "clientrequestid": key,
        "XRoutingParameterSessionKey": key,
        "X-SessionId": session_id,
        "ConversationId": conversation_id,
        "access_token": access_token,
    })
    return "%s/%s@%s?%s" % (WS_BASE, oid, tid, urlencode(q))


def _local_time_zone():
    """This machine's zone, not a constant copied from somebody else's probe.

    The reference implementation hardcodes a timezone and offset. Sending a location the
    machine is not in would be a small, pointless lie in a request that is otherwise honest --
    and it reaches the model, which uses it to resolve "tomorrow" and "this morning".
    """
    off = -(time.altzone if time.daylight and time.localtime().tm_isdst else time.timezone)
    return {"timeZoneOffset": int(off // 3600), "timeZone": time.tzname[0] or "UTC"}


def chat_frames(text: str, *, session_id: str, conversation_id: str, request_id: str,
                started: bool = True, tone: str = "magic", locale: str = "ja-JP",
                gpt_id: str = "", gpt_source: str = "MOS3",
                template: "RequestTemplate" = None, invocation_id: str = "0") -> str:
    """The chat frame and the metrics frame, in one send, as the protocol expects.

    WITH A TEMPLATE the frame is the client's own, with this turn's ids and text written in and
    nothing else touched. That is the shape MEASURED ACCEPTED on 2026-08-20 (`result=Success`,
    and the agent answered using its own tools). Without one, the composed shape below is used;
    it was measured REJECTED, and is kept because the tests exercise it and because the
    difference between composed and captured is the whole lesson.

    SHAPED FROM A LIVE CAPTURE of Microsoft's own client on 2026-08-20, not from the public
    gateway. The gateway's frame is a subset and, critically, leaves `threadLevelGptId` empty --
    which is why it reaches only the default Copilot and has no way to name an agent.

    `gpt_id` names the agent. With it, the agent's own connector applies: its tools are bound
    server-side, so they need no declaration in the prompt, no credential in the request, and
    raise no consent card. Without it the socket reaches the default Copilot, which has neither
    our tools nor tenant grounding.

    `optionsSets` and `allowedMessageTypes` are carried verbatim because the backend's behaviour
    depends on them -- `enable_plugin_auth_interstitial` and `enable_confirmation_interstitial`
    in particular are how tool authorisation and confirmation are surfaced at all.
    """
    if template is not None:
        args = template.frame_for(text, session_id=session_id, request_id=request_id,
                                  started=started)
        chat = {"type": 4, "target": "chat", "invocationId": str(invocation_id), "arguments": [args]}
        metrics = {"type": 1, "target": "Metrics", "arguments": [{"Timestamps": {}}]}
        return json.dumps(chat, ensure_ascii=False) + RS + json.dumps(metrics) + RS

    args = {
        "source": "officeweb",
        "clientCorrelationId": str(uuid.uuid4()),
        "sessionId": session_id,
        "conversationId": conversation_id,
        "traceId": str(uuid.uuid4()),
        "optionsSets": [
            "at_mention_plugins_enable",
            "enable_confirmation_interstitial",
            "enable_plugin_auth_interstitial",
            "enable_request_response_interstitials",
            "enable_response_action_processing",
            "enterprise_flux_image",
            "enterprise_flux_web",
            "enterprise_flux_work",
            "enterprise_toolbox_with_skdsstore",
            "enterprise_pagination_support",
        ],
        "options": {},
        "extraExtensionParameters": {},
        "allowedMessageTypes": [
            "Chat", "Suggestion", "InternalSearchQuery", "Disengaged",
            "InternalLoaderMessage", "Progress", "GeneratedCode",
            "RenderCardRequest", "AdsQuery", "SemanticSerp",
        ],
        "sliceIds": [],
        "isStartOfSession": bool(started),
        "clientInfo": {
            "clientPlatform": "mcmcopilot-web",
            "clientAppName": "Office",
            "clientEntrypoint": "mcmcopilot-officeweb",
            "clientSessionId": session_id,
            "ProductCategory": "Chat",
            "clientAppType": "Web",
            "productEntryPoint": "ChatPanel",
            "deviceOS": "Windows",
            "deviceType": "Desktop",
            "clientPlatformVersion": "10",
        },
        "tone": tone,
        "streamingMode": "ConciseWithPadding",
        "message": {
            "author": "user",
            "inputMethod": "Keyboard",
            "text": text,
            "requestId": request_id,
            "locationInfo": _local_time_zone(),
            "locale": locale,
            "messageType": "Chat",
            "experienceType": "Default",
        },
    }
    # EMPTY MEANS THE DEFAULT COPILOT, and that is a real choice rather than a missing value --
    # so it is written as an empty object exactly like the client does, not omitted.
    args["threadLevelGptId"] = ({"id": gpt_id, "source": gpt_source} if gpt_id else {})
    chat = {"type": 4, "target": "chat", "invocationId": str(invocation_id), "arguments": [args]}
    metrics = {"type": 1, "target": "Metrics", "arguments": [{"Timestamps": {}}]}
    return json.dumps(chat, ensure_ascii=False) + RS + json.dumps(metrics) + RS


def parse_frames(blob):
    """Split one WebSocket message into decoded SignalR frames, skipping what will not parse.

    A frame that is not JSON is not fatal: the stream carries keep-alives and occasional shapes
    this code does not model, and refusing to continue over one of them would turn a cosmetic
    surprise into a lost turn.
    """
    if isinstance(blob, bytes):
        blob = blob.decode("utf-8", "replace")
    out = []
    for part in blob.split(RS):
        if not part:
            continue
        try:
            out.append(json.loads(part))
        except Exception:
            continue
    return out


def _delta_from_payload(payload) -> str:
    """The incremental text this payload appends. Deltas concatenate; snapshots do not."""
    if not isinstance(payload, dict):
        return ""
    return payload["writeAtCursor"] if isinstance(payload.get("writeAtCursor"), str) else ""


def _final_from_payload(payload) -> str:
    """The completed message this payload carries, as a SNAPSHOT of the answer so far."""
    if not isinstance(payload, dict):
        return ""
    chunk = []
    for msg in (payload.get("messages") or []):
        if not isinstance(msg, dict):
            continue
        # Only the assistant's own prose. Progress and loader messages are status, and folding
        # them into the answer would put "searching…" into a coding result.
        if msg.get("messageType") not in (None, "", "Chat"):
            continue
        if msg.get("author") not in (None, "", "bot", "assistant"):
            continue
        text = msg.get("text")
        if isinstance(text, str) and text:
            chunk.append(text)
    return "".join(chunk)


def _payloads(frame):
    """The payload objects in this frame, whichever of the two carriers it uses.

    A StreamInvocation (type 4, which is what a turn is) is answered with SignalR STREAM ITEMS:
    type 2, payload in `item`, no `target` and no `arguments`. Reading only type-1 `update`
    invocations therefore produced an empty answer for a turn that had completed perfectly:
    handshake fine, agent named, completion received, every word discarded. Both appear, so
    both are read.
    """
    t = frame.get("type")
    if t == 2:
        item = frame.get("item")
        return [item] if isinstance(item, dict) else []
    if t == 1 and frame.get("target") == "update":
        return [a for a in (frame.get("arguments") or []) if isinstance(a, dict)]
    return []


def collect_progress(frame) -> list:
    """The messages the ANSWER deliberately excludes, kept instead of dropped.

    MEASURED ON A LIVE RESEARCH, 2026-08-21: the Researcher streams `messageType: "Progress"`
    with `contentOrigin: "ChainOfThoughtSummary"` -- 5,332 characters naming the benchmarks it
    consulted and the numbers it took from each ("RRF combining BM25 and ELSER boosted nDCG@10
    by 1.4% over ELSER alone", "TREC 2025 RAG track ... 0.468 to 0.615"). That is the route the
    research took and the evidence it judged on, and this module was filtering it away to keep
    the prose clean.

    It stays OUT of the answer -- an answer is what the model said, not what it was thinking --
    and it is returned separately so a caller can hand it to another agent and have the claims
    checked instead of taking the report's word for itself.
    """
    out = []
    for payload in _payloads(frame):
        for msg in (payload.get("messages") or []):
            if not isinstance(msg, dict):
                continue
            mt = msg.get("messageType")
            if mt in (None, "", "Chat"):
                continue                      # that is the answer; collect_final has it
            if msg.get("author") not in (None, "", "bot", "assistant"):
                continue
            text = msg.get("text") or msg.get("hiddenText") or ""
            if not isinstance(text, str) or not text:
                continue
            out.append({"type": str(mt), "origin": str(msg.get("contentOrigin") or ""),
                        "text": text})
    return out


def collect_delta(frame) -> str:
    """Incremental text. Concatenating these across a turn rebuilds the answer."""
    return "".join(_delta_from_payload(p) for p in _payloads(frame))


def collect_final(frame) -> str:
    """A completed-message snapshot, if this frame carries one.

    SNAPSHOTS ARE NOT ADDITIVE, and treating them as if they were is how one 166 became
    "166166166": the same answer arrives as a delta stream, as a `messages` array on a type-1
    update, and again on the type-2 stream item. Concatenating all three trebles it.
    """
    return "".join(_final_from_payload(p) for p in _payloads(frame))


def collect_text(frame) -> str:
    """The visible text this frame carries -- its delta if it has one, else its snapshot.

    Kept for callers that look at a single frame. A whole turn must NOT be assembled by
    concatenating this: see `collect_final`.
    """
    return collect_delta(frame) or collect_final(frame)


def result_value(frame) -> str:
    """The backend's verdict on the request, when this frame carries one.

    Observed as `item.result.value` -- "Success", or a reason such as "InvalidRequest". It
    arrives on the same stream item as the answer, so a declined request is not a silent one;
    it only looked silent because nothing here read this field.
    """
    for payload in _payloads(frame):
        res = payload.get("result")
        if isinstance(res, dict) and res.get("value"):
            return str(res["value"])
    return ""


def conversation_id_of(frame) -> str:
    """The conversation id the BACKEND names in this frame, when it names one.

    It arrives alongside the answer, and it is not necessarily the one we asked for -- which
    matters, because a conversation the backend did not create is not one it will continue.
    """
    for payload in _payloads(frame):
        cid = payload.get("conversationId")
        if isinstance(cid, str) and cid:
            return cid
    return ""


def is_complete(frame) -> bool:
    """Whether this frame ends the turn. Type 3 is SignalR's completion."""
    return frame.get("type") == 3


def is_ping(frame) -> bool:
    return frame.get("type") == 6


class Conversation:
    """One socket-backed conversation. Never acquires a token; asks for one.

    `token_supplier()` is called whenever a token is needed and must return a CURRENT access
    token. In this system that supplier reads the signed-in browser -- which is why the browser
    stays, and why this removes rendering rather than removing the browser.
    """

    def __init__(self, token_supplier, *, send_origin=False, user_agent=None,
                 connect_timeout_s=15.0, frame_timeout_s=30.0, turn_timeout_s=300.0,
                 max_frames=2000, max_tool_rounds=16, gpt_id="", template=None):
        if not callable(token_supplier):
            raise ChatHubError(
                "token_supplier must be a callable that returns a token obtained elsewhere. "
                "This module does not acquire tokens, and taking a bare string here would "
                "invite a caller to hardcode one")
        self._token_supplier = token_supplier
        # OPT-IN, and off by default. See the module note: whether the server requires these is
        # a measurement nobody here has taken, and defaulting them on would answer the question
        # by never asking it.
        self.send_origin = bool(send_origin)
        self.user_agent = user_agent
        self.connect_timeout_s = float(connect_timeout_s)
        self.frame_timeout_s = float(frame_timeout_s)
        self.turn_timeout_s = float(turn_timeout_s)
        self.max_frames = int(max_frames)
        self.max_tool_rounds = int(max_tool_rounds)
        #: The agent to talk to. Empty reaches the default Copilot -- no connector, no tenant
        #: grounding, none of our tools.
        self.gpt_id = str(gpt_id or "") or (template.gpt_id if template is not None else "")
        #: The captured request shape. Without one this sends the composed shape, which was
        #: measured rejected -- so production supplies a template and the fleet treats its
        #: absence as "no socket route", not as "try anyway".
        self.template = template
        self.session_id = str(uuid.uuid4())
        self.conversation_id = str(uuid.uuid4())
        #: One key per CONNECTION, not per conversation. The client sends a single value
        #: across chatsessionid, clientrequestid and XRoutingParameterSessionKey, and mints a
        #: fresh one every time it opens a socket -- captured over two consecutive messages in
        #: one conversation, where the key was the only URL parameter that changed. We reused
        #: one key across connections, and every turn after the first was refused.
        self.session_key = str(uuid.uuid4())
        self.turns = 0
        #: What the backend said about the last turn. "Success", or the reason it declined.
        self.last_result = ""
        #: The conversation id the backend used for the last turn, which may not be ours.
        self.server_conversation_id = ""
        #: What the backend said while it was working, from the LAST turn -- progress lines,
        #: search queries, chain-of-thought summaries. Not the answer, and kept apart from it
        #: for that reason. Empty for turns that only ever said one thing.
        self.last_progress = []
        #: The current connection, held only so a failed turn can drop it. A CONVERSATION IS
        #: NOT A CONNECTION here: the client opens a new socket for every message, and keeping
        #: one open was measured WORSE -- with the connection reused, the second turn was
        #: refused (InvalidRequest) and only recovered once the socket had been dropped.
        #: Continuity lives in the conversation id, not in the wire.
        self._sock = None

    def headers(self) -> dict:
        h = {}
        if self.send_origin:
            h["Origin"] = BROWSER_ORIGIN
        if self.user_agent:
            h["User-Agent"] = self.user_agent
        return h

    def ask(self, text: str, *, connect, run_tool=None, catalogue=None, protocol="",
            started=None, on_text=None, on_progress=None):
        """One turn: connect, send, read frames until the turn completes, return the answer.

        `connect(url, headers, timeout_s)` is supplied by the caller and must return an object
        with `send(str)`, `recv(timeout_s) -> str|bytes` and `close()`. Injected for the same
        reason `token_supplier` is: this module has no business choosing a socket library, and
        a seam here is what lets the protocol be tested without a network.

        THE TOOL LOOP RUNS INSIDE THIS CALL and never surfaces. The decision machinery upstream
        reads answers, and a raw fenced block is not an answer -- showing it one would have the
        stuck detector, the refusal detector and the settle check all reasoning about a request
        rather than a reply.
        """
        from relay import socket_tools as ST

        started = self.turns == 0 if started is None else bool(started)
        payload = ST.build_prompt(text, catalogue or [], protocol=protocol) if catalogue             else (protocol or "") + text
        answer, rounds = "", 0
        while True:
            answer = self._one_exchange(payload, connect=connect, started=started,
                                        on_text=on_text, on_progress=on_progress)
            started = False
            self.turns += 1
            if run_tool is None:
                break
            nxt, calls = ST.step(answer, run_tool)
            if nxt is None:
                break
            rounds += 1
            if rounds > self.max_tool_rounds:
                # A model that keeps calling and never concludes is not making progress, and
                # an unbounded loop here would spend a whole goal's budget on one turn.
                raise ChatHubError("tool rounds exceeded %d without a final answer"
                                   % self.max_tool_rounds)
            payload = nxt
        return ST.strip_calls(answer) if catalogue else answer

    def _one_exchange(self, payload: str, *, connect, started: bool, on_text=None,
                      on_progress=None) -> str:
        """Send one payload and read until the turn completes. Returns the reply text.

        `on_text` sees the answer as it grows, so a caller that shows progress does not have
        to wait for the turn. It is display-only and its failures are ignored: a callback that
        raises must not cost a turn that is otherwise fine.
        """
        request_id = str(uuid.uuid4())
        sock = self._connect(request_id, connect)
        try:
            sock.send(chat_frames(payload, session_id=self.session_id,
                                  conversation_id=self.conversation_id,
                                  request_id=request_id, started=started,
                                  gpt_id=self.gpt_id, template=self.template,
                                  invocation_id=str(self.turns)))
            # DELTAS ACCUMULATE, SNAPSHOTS REPLACE. Keeping them apart is what stops the same
            # answer being counted once per channel it arrives on.
            deltas, final, result, seen = [], "", "", 0
            self.last_progress = []
            deadline = time.time() + self.turn_timeout_s
            while time.time() < deadline:
                # A SILENT SOCKET IS NOT A FINISHED TURN. type-3 says the turn completed; it
                # says nothing about a connection that died, so the read is bounded separately
                # and the pings are what prove the far end is still there.
                blob = sock.recv(self.frame_timeout_s)
                if blob is None:
                    raise ChatHubError("the socket went silent before the turn completed")
                for frame in parse_frames(blob):
                    seen += 1
                    if seen > self.max_frames:
                        raise ChatHubError("frame budget exhausted before completion")
                    if is_ping(frame):
                        sock.send(json.dumps({"type": 6}) + RS)
                        continue
                    result = result_value(frame) or result
                    self.server_conversation_id = (conversation_id_of(frame)
                                                   or self.server_conversation_id)
                    for item in collect_progress(frame):
                        self.last_progress.append(item)
                        if on_progress is not None:
                            try:
                                on_progress(item)
                            except Exception:
                                pass
                    deltas.append(collect_delta(frame))
                    final = collect_final(frame) or final
                    if on_text is not None:
                        try:
                            on_text(final or "".join(deltas))
                        except Exception:
                            pass
                    if is_complete(frame):
                        self.last_result = result
                        # A VERDICT IS NOT AN ANSWER. The backend reports why it declined in
                        # `result.value`, and swallowing that produced an empty string that
                        # looked like a quiet model rather than a rejected request -- so the
                        # caller fell back with no reason to record and nothing to fix.
                        if result and result != "Success":
                            raise ChatHubError("the backend declined the request: %s" % result)
                        return final or "".join(deltas)
            raise ChatHubError("turn deadline exceeded before a completion frame")
        except Exception:
            # A TURN THAT FAILED LEAVES THE CONNECTION IN AN UNKNOWN STATE, so it is dropped
            # whatever the policy: reusing it would carry one failure into the next turn.
            self.close()
            raise
        finally:
            self.close()

    def _connect(self, request_id: str, connect):
        """A fresh socket for this turn, handshaken and ready to carry a chat frame."""
        self.session_key = str(uuid.uuid4())     # a key belongs to a connection; see above
        sock = connect(self.url_for_turn(request_id), self.headers(), self.connect_timeout_s)
        sock.send('{"protocol":"json","version":1}' + RS)
        sock.recv(self.frame_timeout_s)              # handshake ack
        self._sock = sock
        return sock

    def close(self):
        """Drop the socket. Safe to call twice, and safe to call on a dead one."""
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            sock.close()
        except Exception:
            pass

    def url_for_turn(self, request_id: str) -> str:
        token = self._token_supplier()
        if not token:
            raise ChatHubError("no token available; the browser session is not signed in")
        if expires_in(token) <= 0:
            raise ChatHubError(
                "the supplied token has expired. Ask the browser for a fresh one -- this "
                "module has no way to renew it, by construction")
        return build_ws_url(token, session_id=self.session_id,
                            conversation_id=self.conversation_id, request_id=request_id,
                            template=self.template, session_key=self.session_key)
