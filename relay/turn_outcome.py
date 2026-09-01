# -*- coding: utf-8 -*-
"""What happened on ONE turn, in exactly one class.

WHY THIS REPLACES SUBSTRING MATCHING OVER WHOLE TRANSCRIPTS. The previous taxonomy -- 55
transport, 53 rate, 25 content across 400 transcripts -- was produced by searching entire files
for phrases. Three things were wrong with it, and each one alone is enough to poison a
controller that learns from the labels:

  1. IT READ OUR OWN PROMPTS. A transcript holds the goal text and every message we send, not
     just the model's replies. Measured: 15 `meta.goal` and 18 `user.text` hits for "rate limit"
     came from a SWE-bench task about a Meraki API returning HTTP 429. The task was ABOUT rate
     limiting; the fleet was fine. Whole-file matching cannot tell those apart.
  2. THE CLASSES OVERLAPPED. One file could count toward all three, so the totals were not a
     partition of anything and the proportions meant nothing.
  3. IT MISSED THE ACTUAL TAXONOMY. Copilot emits a STRUCTURED error, and reading it turns
     guesswork into extraction. Measured over 8,205 assistant turns, 1,273 carry an error code:

         SystemError                        838
         GenAIToolPlannerRateLimitReached   329
         ContextTokenLimitExceeded           46
         AgentBlocked                        30
         OpenAIModelTokenLimit               29
         InfiniteLoopDetected                 1

     The dominant failure is SystemError, two and a half times more common than the rate limit
     everything had been blamed on.

THE CLASSES ARE NOT INTERCHANGEABLE, and that is the whole point of separating them: a rate
refusal means reduce concurrency, a context overflow means send less text and reducing
concurrency does nothing, and a blocked agent is not a capacity signal at all. A controller
that backs off on all of them learns to slow down for reasons that have nothing to do with
load.

ONLY THE ASSISTANT'S OWN REPLY IS CLASSIFIED. Never the goal, never our prompts. That rule is
what removes the contamination above, and it is enforced by the caller passing one turn's text.
"""
from __future__ import annotations

import re

#: One class per turn. Mutually exclusive, and ordered by what a controller should DO.
OK = "ok"                 # a normal reply
RATE = "rate"             # the tenant limiter refused: reduce concurrency
CONTEXT = "context"       # the conversation or model context overflowed: send less, not slower
BLOCKED = "blocked"       # policy / agent blocked: not a capacity signal, never back off
SYSTEM = "system"         # upstream failed generically: retry, do not treat as capacity
LOOP = "loop"             # the service detected a loop in our own driving
UNKNOWN = "unknown"       # an error we have not seen before -- named, never silently bucketed

#: Error code -> class. Extraction, not guesswork: Copilot prints these verbatim.
CODE_CLASS = {
    "GenAIToolPlannerRateLimitReached": RATE,
    "GenAISearchandSummarizeRateLimitReached": RATE,
    "OpenAIRateLimitReached": RATE,
    "ContextTokenLimitExceeded": CONTEXT,
    "OpenAIModelTokenLimit": CONTEXT,
    "AgentBlocked": BLOCKED,
    "InfiniteLoopDetected": LOOP,
    "SystemError": SYSTEM,
}

#: Both localisations of the structured error Copilot renders in the reply body.
_CODE = re.compile(r"(?:エラー\s*コード|Error\s*code)\s*[:：]\s*([A-Za-z][A-Za-z0-9_]{4,60})")

#: Prose that means the model declined. Only consulted when NO structured code is present and
#: the reply is short -- see classify().
_DECLINE = (
    "i can't help", "i cannot help", "i'm unable to", "i am unable to",
    "can't assist with", "cannot assist with", "against my guidelines",
    "お手伝いできません", "対応できません", "サポートしていません",
)

#: A refusal REPLACES the answer, so it is short. Measured: turns carrying an error code have a
#: median length of 146 characters and a p90 of 148, while ordinary working replies run to a
#: median of 155 and a p90 of 874. Prose matching above this length is far more likely to be a
#: worker quoting or discussing an error than being refused -- which is defect (1) above in
#: miniature, so the same mistake is not reintroduced through the fallback.
MAX_DECLINE_CHARS = 400


def error_code(text: str) -> str:
    """The structured error code in this reply, or "". Extraction beats matching: the code is
    emitted by the service, so it cannot be confused with a worker talking about an error."""
    m = _CODE.search(text or "")
    return m.group(1) if m else ""


def classify(text: str, role: str = "assistant"):
    """(class, code) for ONE turn. Exactly one class; `code` is "" when there was no structured
    error.

    ROLE IS CHECKED, NOT ASSUMED. Anything that is not the assistant's own reply is OK by
    definition -- our prompt cannot be a refusal, and treating it as one is precisely how a
    task about HTTP 429 became fifteen phantom rate limits.
    """
    if role != "assistant":
        return OK, ""
    t = text or ""
    code = error_code(t)
    if code:
        return CODE_CLASS.get(code, UNKNOWN), code
    low = t.lower()
    if len(t) <= MAX_DECLINE_CHARS and any(m in low for m in _DECLINE):
        return BLOCKED, ""
    return OK, ""


def classify_turns(records):
    """Classify an iterable of transcript records. Returns [(class, code, record)].

    Takes records rather than a file so the caller decides what a turn is, and so nothing here
    can reach the goal text by accident.
    """
    out = []
    for r in records or []:
        if not isinstance(r, dict):
            continue
        klass, code = classify(r.get("text") or "", r.get("role") or "")
        out.append((klass, code, r))
    return out


def summarise(classified):
    """Counts per class, plus the codes seen. A PARTITION: the classes sum to the turn count.

    The old totals did not, because a transcript could land in three buckets at once, so the
    proportions described nothing.
    """
    counts, codes = {}, {}
    for klass, code, _r in classified or []:
        counts[klass] = counts.get(klass, 0) + 1
        if code:
            codes[code] = codes.get(code, 0) + 1
    counts["_total"] = sum(v for k, v in counts.items() if not k.startswith("_"))
    return {"counts": counts, "codes": codes}


def is_capacity_signal(klass: str) -> bool:
    """Whether this class should make a controller reduce concurrency.

    ONLY `rate`. A context overflow is fixed by sending less text and is unaffected by
    concurrency; a blocked agent and a detected loop are about what we asked, not how much; a
    SystemError is an upstream fault to retry. Treating any of them as backpressure teaches the
    controller from a label that has nothing to do with load -- which is the contamination this
    module exists to end.
    """
    return klass == RATE
