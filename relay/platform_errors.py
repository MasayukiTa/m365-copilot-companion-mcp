# -*- coding: utf-8 -*-
"""Platform error replies: the service answered with an error instead of an answer.

Copilot renders a structured error as the whole reply ("... エラー コード: SystemError。会話 ID:
... 時刻 (UTC): ...", English: "Error code: ..."). It is a fault of the PLATFORM or of the
CONVERSATION, never of the goal, so the harness answers it mechanically, by class:

  TRANSIENT  retry the SAME goal on a FRESH conversation (bounded, with backoff)
  CONTEXT    the conversation / payload is full: continue in a new conversation (the existing
             token-limit recycle, which re-anchors the goal)
  PASS       not handled here (rate limits, blocked agent, detected loop: other handlers own them)
  UNKNOWN    a code nobody has classified: one fresh-conversation retry, then surface the code
             verbatim in the stuck reason

THE ONE TABLE. Adding a code is one line in PLATFORM_ERROR_TABLE: (regex over the whole code,
class). First match wins; no match is UNKNOWN. Nothing else in the harness lists codes.

DOMINANCE. Only a reply that IS the error is classified: it replaces the answer, so it is
short (126-160 characters measured). A long analysis that quotes a code is not the error
(see relay/test_a_worker_that_wrote_about_limits_was_thrown_away.py for the incident).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

TRANSIENT = "transient"
CONTEXT = "context"
PASS = "pass"
UNKNOWN = "unknown"

#: (regex matched against the WHOLE error code, class). First match wins.
PLATFORM_ERROR_TABLE = (
    # size / context limits: continue in a new conversation (existing recycle path)
    (r"ContextTokenLimitExceeded|OpenAIModelTokenLimit|RequestBodyTooLarge|"
     r"AsyncResponsePayloadTooLarge|PayloadTooLarge", CONTEXT),
    # owned by other handlers (throttle path, blocked-agent path, loop detection)
    (r".*RateLimit.*|AgentBlocked|InfiniteLoopDetected", PASS),
    # transient platform faults
    (r"SystemError|UnexpectedError|InternalError|InternalServerError|ServiceUnavailable|"
     r"BadGateway|GatewayTimeout|RequestTimeout|.*Timeout.*|.*TimedOut.*|HTTP_?5\d\d|5\d\d", TRANSIENT),
)

#: A reply longer than this is not "the error", it is prose that mentions one.
MAX_CHARS = 600

#: The structured error as Copilot prints it, in both localisations.
_CODE_RE = re.compile(r"(?:エラー\s*コード|Error\s*code)\s*[:：]\s*([A-Za-z][A-Za-z0-9_]{2,60})", re.I)

_COMPILED = tuple((re.compile(r"(?:%s)\Z" % pat, re.I), cls) for pat, cls in PLATFORM_ERROR_TABLE)


@dataclass(frozen=True)
class PlatformError:
    code: str
    action: str       # TRANSIENT | CONTEXT | PASS | UNKNOWN


def classify_code(code: str) -> str:
    for rx, cls in _COMPILED:
        if rx.match(code or ""):
            return cls
    return UNKNOWN


def detect(text: str):
    """The PlatformError this reply IS, or None when it is an ordinary reply."""
    t = (text or "").strip()
    if not t or len(t) > MAX_CHARS:
        return None
    m = _CODE_RE.search(t)
    if not m:
        return None
    code = m.group(1)
    return PlatformError(code=code, action=classify_code(code))
