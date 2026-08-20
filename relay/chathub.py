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


def build_ws_url(access_token: str, *, session_id: str, conversation_id: str,
                 request_id: str, variants: str = DEFAULT_VARIANTS) -> str:
    """The connection URL. The token travels in the query string -- the server's choice."""
    from urllib.parse import urlencode

    c = claims(access_token)
    oid, tid = c.get("oid"), c.get("tid")
    if not oid or not tid:
        raise ChatHubError("the token carries no oid/tid; it is not a token for this backend")
    q = {
        "chatsessionid": request_id,
        "clientrequestid": request_id,
        "X-SessionId": session_id,
        "ConversationId": conversation_id,
        "access_token": access_token,
        "variants": variants,
        "source": '"officeweb"',
        "product": "Office",
        "agentHost": "Bizchat.FullScreen",
        "agent": "web",
        "scenario": "OfficeWebIncludedCopilot",
    }
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
                started: bool = True, tone: str = "magic", locale: str = "ja-JP") -> str:
    """The chat frame and the metrics frame, in one send, as the protocol expects."""
    chat = {
        "type": 4,
        "target": "chat",
        "invocationId": "0",
        "arguments": [{
            "source": "officeweb",
            "clientCorrelationId": str(uuid.uuid4()),
            "sessionId": session_id,
            "conversationId": conversation_id,
            "traceId": str(uuid.uuid4()),
            "optionsSets": [],
            "options": {},
            "sliceIds": [],
            "threadLevelGptId": {},
            "allowedMessageTypes": ["Chat", "Suggestion", "Disengaged", "Progress",
                                    "EndOfRequest", "InternalLoaderMessage"],
            "isStartOfSession": bool(started),
            "productThreadType": "Office",
            "clientInfo": {"clientPlatform": "mcmcopilot-web", "clientAppName": "Office"},
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
        }],
    }
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


def collect_text(frame) -> str:
    """The visible text this frame adds, if any."""
    if frame.get("type") != 1 or frame.get("target") != "update":
        return ""
    chunk = []
    for arg in frame.get("arguments") or []:
        if not isinstance(arg, dict):
            continue
        if isinstance(arg.get("writeAtCursor"), str):
            chunk.append(arg["writeAtCursor"])
    return "".join(chunk)


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
                 max_frames=2000):
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
        self.session_id = str(uuid.uuid4())
        self.conversation_id = str(uuid.uuid4())
        self.turns = 0

    def headers(self) -> dict:
        h = {}
        if self.send_origin:
            h["Origin"] = BROWSER_ORIGIN
        if self.user_agent:
            h["User-Agent"] = self.user_agent
        return h

    def url_for_turn(self, request_id: str) -> str:
        token = self._token_supplier()
        if not token:
            raise ChatHubError("no token available; the browser session is not signed in")
        if expires_in(token) <= 0:
            raise ChatHubError(
                "the supplied token has expired. Ask the browser for a fresh one -- this "
                "module has no way to renew it, by construction")
        return build_ws_url(token, session_id=self.session_id,
                            conversation_id=self.conversation_id, request_id=request_id)
