# -*- coding: utf-8 -*-
"""One turn, one class -- so a controller cannot learn from contaminated labels.

WHAT THE OLD TAXONOMY WAS. "55 transport, 53 rate, 25 content across 400 transcripts", produced
by searching whole files for phrases. Every part of that was wrong:

  - it read our OWN prompts and the goal text, not just the model's replies. Fifteen of the
    "rate limit" hits came from a SWE-bench task about a Meraki API returning HTTP 429 -- the
    task was ABOUT rate limiting and the fleet was fine;
  - the classes overlapped, so the numbers were not a partition and the proportions meant
    nothing;
  - and it missed the real taxonomy entirely. Copilot emits a structured error code. Over 8,205
    assistant turns: SystemError 838, GenAIToolPlannerRateLimitReached 329,
    ContextTokenLimitExceeded 46, AgentBlocked 30, OpenAIModelTokenLimit 29,
    InfiniteLoopDetected 1. The dominant failure is SystemError -- two and a half times the
    rate limit everything had been blamed on.
"""
import pytest

from relay import turn_outcome as T


RATE_REPLY = ("エラーが発生しました。 エラー コード: GenAIToolPlannerRateLimitReached "
              "会話 ID: 81135540-3a67-4e23-8865-d72558cc2958 時刻 (UTC): 2026-08-27T04:01:23.981Z。")


# -- extraction, not guesswork --------------------------------------------------------------

@pytest.mark.parametrize("code,klass", [
    ("GenAIToolPlannerRateLimitReached", T.RATE),
    ("OpenAIRateLimitReached", T.RATE),
    ("ContextTokenLimitExceeded", T.CONTEXT),
    ("OpenAIModelTokenLimit", T.CONTEXT),
    ("AgentBlocked", T.BLOCKED),
    ("InfiniteLoopDetected", T.LOOP),
    ("SystemError", T.SYSTEM),
])
def test_each_documented_code_maps_to_its_own_class(code, klass):
    assert T.classify("エラー コード: " + code) == (klass, code)


def test_the_english_rendering_is_read_too():
    assert T.classify("An error occurred. Error code: SystemError") == (T.SYSTEM, "SystemError")


def test_a_real_refusal_from_the_transcripts():
    assert T.classify(RATE_REPLY) == (T.RATE, "GenAIToolPlannerRateLimitReached")


def test_an_unseen_code_is_named_not_silently_bucketed():
    """A new error must not be quietly counted as something we already understand. UNKNOWN with
    the code attached is the honest answer and the thing that makes the next one findable."""
    klass, code = T.classify("エラー コード: SomeBrandNewFailure")
    assert klass == T.UNKNOWN and code == "SomeBrandNewFailure"


def test_a_normal_reply_is_ok():
    assert T.classify("I read the file and applied the patch. Tests pass.") == (T.OK, "")


# -- the contamination this exists to end ----------------------------------------------------

def test_the_goal_text_is_never_classified():
    """THE MEASURED DEFECT. A SWE-bench task about a Meraki API returning HTTP 429 produced
    fifteen phantom rate limits, because whole-file matching cannot tell our own prompt from
    the model's reply."""
    task = ("When Meraki modules interact with the Meraki API and the service returns HTTP 429 "
            "(rate limited) or transient server errors, playbook tasks stop with an error.")
    assert T.classify(task, role="meta") == (T.OK, "")
    assert T.classify(task, role="user") == (T.OK, "")


def test_a_worker_discussing_rate_limiting_is_not_a_refusal():
    """The same defect from the other side: a long assistant reply that MENTIONS rate limiting
    while doing the work is a worker doing its job, not a worker being refused."""
    long_reply = ("I added a retry with backoff for HTTP 429 rate limited responses. " * 12)
    assert len(long_reply) > T.MAX_DECLINE_CHARS
    assert T.classify(long_reply) == (T.OK, "")


def test_prose_decline_counts_only_when_the_reply_is_short():
    """A refusal REPLACES the answer, so it is short -- measured median 146 characters against
    a p90 of 874 for ordinary replies. Matching prose in a long reply reintroduces exactly the
    contamination the structured path removed."""
    assert T.classify("I can't help with that.") == (T.BLOCKED, "")
    padded = "Here is my analysis. " * 40 + "I can't help with that."
    assert len(padded) > T.MAX_DECLINE_CHARS
    assert T.classify(padded) == (T.OK, "")


def test_a_structured_code_beats_prose():
    """When both are present the machine-emitted code is the cause; the prose is commentary."""
    both = "I can't help with that. エラー コード: GenAIToolPlannerRateLimitReached"
    assert T.classify(both) == (T.RATE, "GenAIToolPlannerRateLimitReached")


# -- the classes must partition ---------------------------------------------------------------

def test_every_turn_lands_in_exactly_one_class():
    """The old totals did not add up to anything: one transcript could count toward all three
    buckets, so the proportions described nothing."""
    records = [
        {"role": "assistant", "text": RATE_REPLY},
        {"role": "assistant", "text": "エラー コード: SystemError"},
        {"role": "assistant", "text": "done"},
        {"role": "user", "text": "rate limit rate limit"},
        {"role": "meta", "text": "throttled"},
    ]
    s = T.summarise(T.classify_turns(records))
    assert s["counts"]["_total"] == len(records)
    assert s["counts"][T.RATE] == 1
    assert s["counts"][T.SYSTEM] == 1
    assert s["counts"][T.OK] == 3
    assert s["codes"] == {"GenAIToolPlannerRateLimitReached": 1, "SystemError": 1}


def test_non_dict_records_are_skipped_not_crashed():
    assert T.classify_turns([None, "x", {"role": "assistant", "text": "ok"}]) != []


# -- what a controller may act on -------------------------------------------------------------

def test_only_a_rate_refusal_is_backpressure():
    """A context overflow is fixed by sending less text and is unaffected by concurrency; a
    blocked agent and a detected loop are about WHAT we asked, not how much; a SystemError is an
    upstream fault to retry. Backing off for any of them teaches the controller from a label
    that has nothing to do with load."""
    assert T.is_capacity_signal(T.RATE) is True
    for klass in (T.CONTEXT, T.BLOCKED, T.SYSTEM, T.LOOP, T.OK, T.UNKNOWN):
        assert T.is_capacity_signal(klass) is False, klass


def test_the_dominant_failure_is_not_treated_as_capacity():
    """SystemError is 838 of 1,273 observed errors. If it counted as backpressure the
    controller would spend most of its life throttling itself for an upstream fault that
    concurrency cannot affect."""
    klass, _ = T.classify("エラー コード: SystemError")
    assert klass == T.SYSTEM
    assert T.is_capacity_signal(klass) is False
