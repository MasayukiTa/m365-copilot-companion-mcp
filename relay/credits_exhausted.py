# -*- coding: utf-8 -*-
"""The one-off reply that means the Copilot Studio ENVIRONMENT is out of credits.

WHY ITS OWN DETECTOR, NOT A platform_errors ROW. A credit exhaustion is not the structured
`エラー コード: <Code>` reply that relay/platform_errors.py reads -- it is plain prose the
service returns in place of an answer ("You need credits to continue ... This environment is
out of credits"). platform_errors.detect() only fires on the structured code, so this reply
fell through to the generic text handlers and was settled as a product failure. It is not one:
the environment has no capacity assigned, which cannot be fixed on this PC, and no amount of
retrying or rewording changes it.

THE RULE IS TWO PARTS, the same shape as turn_outcome's decline fallback: a distinctive marker
AND a short reply. A credit notice REPLACES the answer, so it is short; a long reply that
mentions "credits" is far more likely to be a worker quoting or discussing the word than the
service refusing for lack of them -- the whole-transcript contamination turn_outcome.py exists
to avoid, kept out here the same way.

ONLY THE ASSISTANT'S OWN REPLY is ever passed here (the caller enforces it, exactly as for
turn_outcome.classify): our own prompt must never be read as a credit notice.
"""
from __future__ import annotations

#: The single plain line the doctor/bridge path prints and the cockpit shows, verbatim, in the
#: same words in both places. ASCII so it is safe in every surface (logs, status.json, the C#
#: cockpit). Kept here as the one source of the sentence.
MESSAGE = (
    "The Copilot Studio environment is out of credits; ask the environment administrator "
    "to assign capacity or a pay-as-you-go plan; nothing on this PC needs to change"
)

#: A credit notice replaces the answer, so it is short. Measured bound mirrors
#: turn_outcome.MAX_DECLINE_CHARS: prose longer than this that merely contains the marker is a
#: worker discussing credits, not the service withholding an answer for lack of them.
MAX_CHARS = 400

#: Distinctive phrases the service uses for this state. Each is specific enough that it does not
#: occur in our prompts or in ordinary working replies. Matched case-insensitively. A reply
#: needs ONE of these AND to be within MAX_CHARS.
MARKERS = (
    "out of credits",
    "need credits to continue",
    "you need credits",
    "run out of credits",
    "no credits",
    "ran out of credits",
    "assign capacity",
    "pay-as-you-go",
    "pay as you go",
)


def detect(text: str) -> bool:
    """True when this reply is the environment-out-of-credits notice. False otherwise.

    Two parts, both required: a distinctive marker, and a reply short enough that the marker
    stands in place of an answer rather than being discussed inside one. An empty or missing
    reply is never a credit notice.
    """
    t = (text or "").strip()
    if not t or len(t) > MAX_CHARS:
        return False
    low = t.lower()
    return any(m in low for m in MARKERS)
