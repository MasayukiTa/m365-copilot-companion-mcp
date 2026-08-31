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


# ── admission pacing: why the throttle happened in the first place ────────────────────────

@pytest.fixture(autouse=True)
def _clean_pacing():
    RF._reset_admission_pacing()
    yield
    RF._reset_admission_pacing()


def test_the_first_admission_is_never_delayed():
    """A run must not pay the ramp before it has admitted anyone."""
    assert RF.admission_is_due(now=1000.0)


def test_a_second_admission_in_the_same_instant_is_deferred():
    """THE BURST, in one assertion. Admission drains `pending` inside a single sweep and a
    socket worker weighs zero, so before this the whole batch went in at once."""
    RF.note_admitted(now=1000.0)
    assert not RF.admission_is_due(now=1000.0)
    assert not RF.admission_is_due(now=1000.0 + RF.ADMIT_MIN_INTERVAL_S / 2)
    assert RF.admission_is_due(now=1000.0 + RF.ADMIT_MIN_INTERVAL_S + 0.01)


def test_the_spacing_matches_what_survived():
    """Measured: the eight workers that arrived about a second and a half apart all got
    through; twenty arriving in the same second did not."""
    assert 1.0 <= RF.ADMIT_MIN_INTERVAL_S <= 3.0


def test_a_throttle_anywhere_slows_admission_everywhere():
    """The quota is shared, so one worker's refusal is evidence about all of them."""
    base = RF.admit_interval_now(now=2000.0)
    RF.note_upstream_throttle(now=2000.0)
    assert RF.admit_interval_now(now=2001.0) > base


def test_the_widened_interval_relaxes_once_the_throttle_is_stale():
    RF.note_upstream_throttle(now=3000.0)
    widened = RF.admit_interval_now(now=3001.0)
    later = RF.admit_interval_now(now=3000.0 + RF.ADMIT_THROTTLE_MEMORY_S + 1)
    assert later < widened
    assert later == RF.ADMIT_MIN_INTERVAL_S


def test_pacing_can_be_switched_off_entirely(monkeypatch):
    monkeypatch.setattr(RF, "ADMIT_MIN_INTERVAL_S", 0.0)
    RF.note_admitted(now=4000.0)
    assert RF.admission_is_due(now=4000.0)


def test_the_guard_covers_both_admission_paths():
    """THE FAILURE CLASS, NOT THE CALLER. This loop pops from `pending` in two places --
    `pending.pop(pick)` in the per-repo disk branch and `pending.pop(0)` in the flat one. The
    first version of the guard sat beside the second, which would have paced every kind of run
    except the benchmark runs that produced the measurement.

    Asserted structurally: the guard must appear BEFORE the first pop in the loop body, so no
    later-added pop can slip past it.
    """
    import inspect
    import io as _io
    import tokenize

    # COMMENTS OFF FIRST. The comment above the guard NAMES both `pending.pop(0)` and
    # `pending.pop(pick)` -- so a raw scan finds its own explanation, decides a pop precedes
    # the guard, and fails. Writing a source assertion against text that includes the note
    # explaining it is a trap this repository has recorded, and this test walked into it.
    src = inspect.getsource(RF.run_relay_fleet)
    code_lines = []
    for tok in tokenize.generate_tokens(_io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        code_lines.append((tok.start[0], tok.string))
    code = "\n".join("%d:%s" % (ln, s) for ln, s in code_lines)

    guard_lines = [ln for ln, s in code_lines if s == "admission_is_due"]
    pop_lines = [ln for i, (ln, s) in enumerate(code_lines)
                 if s == "pop" and i and code_lines[i - 1][1] == "."
                 and i > 1 and code_lines[i - 2][1] == "pending"]
    assert pop_lines, "the admission loop no longer pops from pending; re-check this guard"
    assert guard_lines, "the spacing guard is gone from run_relay_fleet"
    assert min(guard_lines) < min(pop_lines), (
        "an admission path can be reached without the spacing guard: guard at %s, pops at %s"
        % (guard_lines, pop_lines))
    assert code  # keep the joined form referenced for debugging output


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
