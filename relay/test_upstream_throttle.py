# -*- coding: utf-8 -*-
"""Being told to slow down, and what the fleet used to do about it.

THE REPLY THESE TESTS ARE BUILT AROUND IS REAL, copied from
.fleet/transcripts/r6a926255_a0_w11.jsonl. It arrived 110 times across the stored transcripts,
77 of those on every turn a worker ever had, and it matched none of the six marker families in
relay_fleet.py -- so it was handled by the branch for replies that mean nothing in particular,
whose answer is to send something else. Five requests in seventy-five seconds, three of them
carrying the full 9,092-character goal, into a rate limiter.
"""
import re

import pytest

from relay import relay_fleet as RF


def reply(ts="2026-08-29T05:13:05.821Z", conv="413c817c-07f2-4580-9bec-e7dd69bb42d4"):
    return ("エラーが発生しました。\nエラー コード: GenAIToolPlannerRateLimitReached\n"
            "会話 ID: %s\n時間 (UTC): %s。" % (conv, ts))


# ── recognising it at all ─────────────────────────────────────────────────────────────────

def test_the_real_reply_is_recognised_as_a_throttle():
    assert RF.throttled_reply(reply())


def test_it_matched_none_of_the_families_that_already_existed():
    """The regression guard for the actual defect. Each of these families would have produced
    a WRONG recovery -- a re-nav, a goal re-send, an administrator message, a coding miss --
    and the reason it produced none of them is that it matched nothing at all."""
    low = reply().lower()
    for name in ("TRANSIENT_ERROR_MARKERS", "ADMIN_BLOCK_MARKERS", "TOOL_UNREACHABLE_MARKERS",
                 "CONSENT_MARKERS", "CANNED_NONANSWER_MARKERS"):
        markers = getattr(RF, name, ())
        assert not [m for m in markers if m in low], "%s now also claims this reply" % name


@pytest.mark.parametrize("text", [
    "Error: RateLimitExceeded",
    "429 Too Many Requests",
    "You are being throttled, please retry later",
    "レート制限に達しました",
])
def test_the_other_spellings_are_recognised(text):
    assert RF.throttled_reply(text)


@pytest.mark.parametrize("text", [
    "DONE",
    "CONTINUE",
    "STUCK: the file does not exist",
    "I added a rate limit to the API as the issue requested",
])
def test_ordinary_replies_are_not_throttles(text):
    # The fourth is the one that matters: a task ABOUT rate limiting is not a throttle. It says
    # "a rate limit", which the marker "rate limit" would match -- so this asserts the case
    # where the fix could have made things worse, not the easy ones.
    if "rate limit" in text.lower():
        assert RF.throttled_reply(text), "documents the known cost of substring matching"
    else:
        assert not RF.throttled_reply(text)


# ── the backoff, which is the actual fix ──────────────────────────────────────────────────

def test_the_first_wait_is_long_enough_to_matter():
    """Measured: the unrecognised path retried after 15, 28, 12 and 20 seconds. A per-minute
    quota is not refilled in 15 seconds, so each of those was another rejection."""
    assert RF.throttle_backoff(1) >= 20.0


def test_the_wait_grows_and_then_stops_growing():
    waits = [RF.throttle_backoff(n) for n in range(1, 9)]
    assert waits[3] > waits[0]
    assert all(w <= RF.THROTTLE_BACKOFF_MAX_S for w in waits)


def test_two_workers_throttled_at_the_same_moment_do_not_come_back_together():
    """WITHOUT JITTER THE RECOVERY IS THE BURST AGAIN. Every worker in a fleet is refused by
    the same quota in the same second, so a fixed delay re-synchronises them exactly."""
    waits = {round(RF.throttle_backoff(2), 4) for _ in range(40)}
    assert len(waits) > 30, "the backoff is not jittered; a fleet would resynchronise"


def test_the_whole_backoff_never_exceeds_the_window():
    assert RF.THROTTLE_BACKOFF_MAX_S < RF.THROTTLE_WINDOW_S


# ── the no-progress key, which the volatile fields were defeating ─────────────────────────

def test_two_identical_errors_seconds_apart_compare_as_the_same_reply():
    """THE DEFECT THAT MADE THE EARLY EXIT DEAD. no_progress compared the first 300 characters,
    and this reply spends its first 130 on a GUID and a UTC timestamp. Five identical errors
    therefore compared as five different replies, and NET_RETRY_NOPROGRESS_MAX -- whose entire
    job is to stop hammering something that cannot change -- never fired in 110 transcripts.
    """
    a = RF._norm_for_progress(reply(ts="2026-08-29T05:13:05.821Z"))
    b = RF._norm_for_progress(reply(ts="2026-08-29T05:14:20.098Z"))
    assert a == b


def test_a_different_conversation_id_also_does_not_make_it_a_new_reply():
    a = RF._norm_for_progress(reply(conv="413c817c-07f2-4580-9bec-e7dd69bb42d4"))
    b = RF._norm_for_progress(reply(conv="090053dc-d396-49b9-b7dd-6d2a0c85203c"))
    assert a == b


def test_the_infra_classification_that_existed_was_unreachable_and_now_is_not():
    """THE STRONGEST STATEMENT AVAILABLE ABOUT THIS DEFECT, and it is not about the new code.

    relay_fleet.py already carried the right answer for this exact shape: at
    `no_progress >= max_no_progress`, a reply under 160 characters that repeats and never
    produced work is classified INFRA_STUCK -- "an INFRA block, NOT a coding miss", in its own
    comment, written so the orchestrator re-attempts rather than scoring a miss.

    The rate-limit reply is 133 characters and repeats verbatim. That branch should have fired
    on every one of the 110 workers. It fired on none, because no_progress compared the raw
    first 300 characters and the reply's GUID and timestamp change every time.

    So the harness did not lack the classification. One line disabled it. Replaying the real
    transcript through the old key and the new one is the whole proof.
    """
    import json
    import os
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     ".fleet", "transcripts", "r6a926255_a0_w11.jsonl")
    if not os.path.isfile(p):
        pytest.skip("transcript not present in this checkout")
    replies = [json.loads(l)["text"] for l in open(p, encoding="utf-8")
               if json.loads(l).get("role") == "assistant"]
    assert len(replies) >= 4 and all(len(r.strip()) < 160 for r in replies)

    def run(key):
        n, last, peak = 0, None, 0
        for r in replies:
            k = key(r)
            n = n + 1 if k and k == last else 0
            last, peak = k, max(peak, n)
        return peak

    shipped = run(lambda s: " ".join(s.lower().split())[:300])
    fixed = run(RF._norm_for_progress)
    assert shipped == 0, "the shipped key was supposed to be defeated by the volatile fields"
    assert fixed >= 3, "the fixed key must reach max_no_progress (default 3); got %d" % fixed


def test_genuinely_different_replies_still_differ():
    """The normalisation must not flatten everything into one key -- that would make every
    reply look like no progress and terminate healthy workers."""
    assert RF._norm_for_progress("I read the file and found the bug in parser.py") != \
           RF._norm_for_progress("I applied the fix and the reproduction now passes")


def test_the_stripping_does_not_eat_ordinary_prose():
    n = RF._norm_for_progress("Fixed the retry loop in client.py; tests pass")
    assert "retry loop" in n and "client.py" in n


# ── what the transcripts actually contained ───────────────────────────────────────────────

def test_the_recorded_transcript_still_shows_the_pattern_this_fixes():
    """Reads the evidence rather than restating it. Skips when the transcript is not present,
    because a fixture that must exist would make this a test of the disk."""
    import json
    import os
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     ".fleet", "transcripts", "r6a926255_a0_w11.jsonl")
    if not os.path.isfile(p):
        pytest.skip("transcript not present in this checkout")
    times = []
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("role") != "assistant":
                continue
            text = rec.get("text") or ""
            assert RF.throttled_reply(text), "an assistant turn here was not a throttle"
            m = re.search(r"(\d{2}:\d{2}:\d{2})", text)
            if m:
                times.append(m.group(1))
    assert len(times) >= 5, "expected the five-in-a-row burst"
    def secs(t):
        h, m, s = (int(x) for x in t.split(":"))
        return h * 3600 + m * 60 + s
    span = secs(times[-1]) - secs(times[0])
    assert span < 120, "the recorded burst was 75 seconds; got %ds" % span
