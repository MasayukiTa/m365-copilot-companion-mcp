# -*- coding: utf-8 -*-
"""Every non-ok turn records what class it was (codex-plan follow-up, item 7).

relay/turn_outcome.py partitions one assistant turn by the STRUCTURED error code Copilot emits.
It was built from a measurement of 8,205 turns, it is careful about the contamination that
turned a SWE-bench task ABOUT HTTP 429 into fifteen phantom rate limits -- and it had ZERO
callers, so its taxonomy could only ever be recomputed offline, never watched.

WHY THIS SHIPS AS INSTRUMENTATION AND NOT AS A CONTROL BRANCH. A first pass surveyed only the
six named marker families in relay_fleet.py and concluded 171 turns were invisible to the fleet.
That was a FALSE NEGATIVE: conversation_exhausted (copilot_autopilot_relay.py:642) is a seventh
handler, it is checked first in _decide, and it has matched contexttokenlimitexceeded since a
real 2026-08-26 incident. Re-measured over 1741 transcripts / 4468 assistant turns:

    GenAIToolPlannerRateLimitReached  227   THROTTLE_MARKERS
    ContextTokenLimitExceeded         165   conversation_exhausted -> RECYCLE
    SystemError                        29   TRANSIENT_ERROR_MARKERS
    InfiniteLoopDetected                1   TOOL_UNREACHABLE_MARKERS (misread, n=1)
    AsyncResponsePayloadTooLarge        5   <UNHANDLED>
    RequestBodyTooLarge                 1   <UNHANDLED>

Six turns reach no handler. Following those six workers through their own transcripts: FIVE
recovered and went on to finish real work; ONE ended on the error. A terminal branch would
therefore have killed five workers that recovered in order to rescue one -- a draft of exactly
that branch was written and reverted on this evidence.

So what ships is what the evidence supports: the class is recorded per turn, which turns "six,
probably harmless" into something a later decision can be made from.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import relay_fleet as RF        # noqa: E402
from relay import turn_outcome as TO       # noqa: E402
from relay.copilot_autopilot_relay import conversation_exhausted  # noqa: E402

#: Real texts, copied verbatim out of the transcripts named in the module docstring.
REAL_CONTEXT = ("error occurred. Error code: ContextTokenLimitExceeded "
                "conversation ID: 3a4e9cc0-65a7-49b5-a326-9fdc8c6a07d2")
REAL_RATE = ("error occurred. Error code: GenAIToolPlannerRateLimitReached "
             "conversation ID: 9426a7c8-fec2-45f0-b2bc-cabe10150235")
REAL_OVERSIZE = ("error occurred. Error code: AsyncResponsePayloadTooLarge "
                 "conversation ID: 1ad6bd23-c500-4461-9480-24c4e5640b5a")


class _Tx:
    """Captures what _decide records, without needing a transcript file."""

    def __init__(self):
        self.metrics = []

    def assistant(self, turn, text):
        pass

    def metric(self, turn, name, value, **kw):
        self.metrics.append((turn, name, value, kw))


def _worker(monkeypatch):
    w = RF.RelayWorker("investigate the thing", "w0")
    monkeypatch.setattr(w, "_tx", _Tx(), raising=False)
    return w, w._tx


# ── what ships: the class is recorded ─────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected,code", [
    (REAL_CONTEXT, TO.CONTEXT, "ContextTokenLimitExceeded"),
    (REAL_RATE, TO.RATE, "GenAIToolPlannerRateLimitReached"),
    (REAL_OVERSIZE, TO.CONTEXT, "AsyncResponsePayloadTooLarge"),
])
def test_a_failing_turn_records_its_class_and_code(monkeypatch, text, expected, code):
    w, tx = _worker(monkeypatch)
    w._decide(text)
    rows = [m for m in tx.metrics if m[1] == "turn_class"]
    assert rows, "the turn class was not recorded at all"
    assert rows[0][2] == expected
    assert rows[0][3].get("code") == code


def test_an_ordinary_reply_is_not_recorded():
    """Only non-ok turns are written. A row for all 4040 ok turns would bury the 428 that say
    something, and this stream is read by a person."""
    assert TO.classify("done, the file was updated", "assistant") == (TO.OK, "")


def test_recording_can_never_fail_the_turn(monkeypatch):
    """A measurement side-channel must not be able to break the thing it measures."""
    w, _tx = _worker(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("classifier exploded")
    monkeypatch.setattr(TO, "classify", _boom)
    w._decide(REAL_RATE)                      # must not raise
    assert w._throttle_streak == 1, "the throttle path stopped working"


def test_our_own_prompt_is_never_classified_as_a_failure():
    """THE CONTAMINATION turn_outcome EXISTS TO END: fifteen phantom rate limits came from a
    SWE-bench task that was ABOUT HTTP 429. _decide passes the role explicitly for this."""
    prose = ("The failure is a ContextTokenLimitExceeded on their side; I will summarise the "
             "file before sending it so the request stays under the model token limit.")
    assert TO.classify(prose, "assistant")[0] == TO.OK
    assert TO.classify(REAL_CONTEXT, "user")[0] == TO.OK


# ── the two codes added to the taxonomy, and why ──────────────────────────────────────────

@pytest.mark.parametrize("code", ["RequestBodyTooLarge", "AsyncResponsePayloadTooLarge"])
def test_the_two_measured_size_codes_are_classified_not_unknown(code):
    """These six turns were the ENTIRE `unknown` bucket on real data (5 and 1). UNKNOWN is a
    class nothing acts on and no reader looks for, and these reached real workers."""
    klass, got = TO.classify("error occurred. Error code: %s" % code, "assistant")
    assert klass == TO.CONTEXT and got == code


def test_a_size_refusal_is_never_a_capacity_signal():
    """If one became a capacity signal, a controller would cut concurrency for a condition
    concurrency cannot affect -- the contamination this module was built to stop."""
    for code in ("ContextTokenLimitExceeded", "RequestBodyTooLarge",
                 "AsyncResponsePayloadTooLarge", "OpenAIModelTokenLimit"):
        klass, _ = TO.classify("Error code: %s" % code, "assistant")
        assert TO.is_capacity_signal(klass) is False
    assert TO.is_capacity_signal(TO.RATE) is True


def test_the_classes_still_partition():
    rows = [{"role": "assistant", "text": REAL_CONTEXT},
            {"role": "assistant", "text": REAL_RATE},
            {"role": "assistant", "text": "ordinary reply"},
            {"role": "user", "text": "our prompt mentioning ContextTokenLimitExceeded"}]
    s = TO.summarise(TO.classify_turns(rows))
    assert s["counts"]["_total"] == len(rows)
    assert s["counts"][TO.OK] == 2


# ── the handlers this deliberately does NOT duplicate ─────────────────────────────────────

def test_a_full_conversation_is_already_recycled_by_an_existing_handler():
    """THE FALSE NEGATIVE THAT ALMOST SHIPPED A SECOND HANDLER. 165 of these turns exist and
    conversation_exhausted has matched them since 2026-08-26. A new branch for them would have
    been a competing answer to a question already answered -- one fact, two sources."""
    assert conversation_exhausted(REAL_CONTEXT) is True
    assert conversation_exhausted(REAL_RATE) is False


def test_the_unhandled_codes_really_are_unhandled():
    """Pins the gap honestly: these reach neither the recycle nor the throttle. Six turns on
    record, five of whose workers recovered anyway, which is why nothing acts on them yet. If a
    handler is ever added, this is where the claim gets updated."""
    assert conversation_exhausted(REAL_OVERSIZE) is False
    assert RF.throttled_reply(REAL_OVERSIZE) is False
    assert TO.classify(REAL_OVERSIZE, "assistant")[0] == TO.CONTEXT


def test_a_rate_limit_still_takes_the_throttle_path(monkeypatch):
    """Recording must not disturb the branch that follows it."""
    w, _tx = _worker(monkeypatch)
    w._decide(REAL_RATE)
    assert w.status == "ready" and w._throttle_streak == 1 and w._cooldown_until > 0
