"""Did the worker follow the approved procedure, or invent its own?

WHY A GRADER AND NOT A READING. The first attempt at this experiment was judged by reading
three answers and deciding they looked alike. That is the shape of judgement that finds
whatever it went looking for, and it is why the result could not be defended afterwards. The
question here has a mechanical answer, so it gets a mechanical grader written BEFORE the
arms run.

WHAT MAKES THIS PROBE DISCRIMINATING, where the first one was not. The first probe asked for a
count that was reachable without consulting any skill: all three arms produced it, agreed, and
were right. Agreement told us nothing, because following the procedure and ignoring it lead to
the same answer.

`mail-lookup` encodes two decisions a worker will not arrive at on its own:

  * TERMINATION IS `hasMoreResults`, not "the page looked short". The skill says so in as many
    words -- "「もう無さそう」で止めない" -- because stopping early produces ranges that are
    reported as complete and are not.
  * THE PERIOD IS SPLIT FIRST, in dates, and the splits are listed before fetching. Not
    "continue from where I left off", which breaks when several conversations run at once.

Both are visible in the ANSWER: a worker that followed them reports per-range counts with the
evidence that each range terminated. A worker that did not cannot produce that shape by
accident -- which is precisely what "discriminating" means here, and precisely what the first
probe lacked.

WHAT THIS DOES NOT MEASURE. Whether the counts are correct. A disciplined worker can still be
wrong, and a lucky one can be right; those are different questions and mixing them is how the
first experiment lost its own conclusion.
"""
from __future__ import annotations

import re

#: The termination evidence the skill requires. Written as alternatives because the worker
#: reports in Japanese and may or may not quote the field name in Latin.
_TERMINATION = (
    re.compile(r"hasMoreResults", re.IGNORECASE),
    re.compile(r"終端(確認|を確認|済)"),
)

#: A range reported as empty must say so as a CHECKED zero. The skill is explicit that
#: "0件" and "調べていない" are not the same claim.
_CHECKED_ZERO = re.compile(r"(受信なし|該当0件|0件[（(]確認)")

#: A date range written as dates. "残りを続ける" is what the skill forbids.
_DATE_RANGE = re.compile(
    r"\d{1,2}\s*[/月]\s*\d{1,2}\s*[日]?\s*[~〜\-–]\s*\d{1,2}\s*[/月]?\s*\d{1,2}")

#: The phrases the skill tells the worker NOT to use, because a parallel conversation makes
#: them ambiguous.
_FORBIDDEN_CONTINUATION = re.compile(r"(残りを続け|続きから|前回の続き)")


def grade(answer: str) -> dict:
    """Score one answer against the procedure. Returns the signals and a total.

    Every signal is a fact about the text, not a judgement about the work, so two people
    grading the same answer get the same number.
    """
    text = answer or ""
    ranges = _DATE_RANGE.findall(text)
    signals = {
        # The skill's own termination rule, quoted or named.
        "cites_termination": any(p.search(text) for p in _TERMINATION),
        # Ranges written as dates, and more than one of them -- a single range is what a
        # worker who never split would also produce.
        "split_into_dated_ranges": len(ranges) >= 2,
        "n_dated_ranges": len(ranges),
        # An empty range reported as a checked zero rather than as silence.
        "checked_zero_when_empty": bool(_CHECKED_ZERO.search(text)),
        # The phrasing the skill forbids.
        "used_forbidden_continuation": bool(_FORBIDDEN_CONTINUATION.search(text)),
        # Did it say it consulted a skill at all? Weakest signal, and deliberately not the
        # only one: saying "I used the skill" is a claim, and the other signals are evidence.
        "mentions_skill_lookup": bool(re.search(r"skill_(match|load)", text)),
    }
    score = sum(1 for k in ("cites_termination", "split_into_dated_ranges",
                            "checked_zero_when_empty") if signals[k])
    if signals["used_forbidden_continuation"]:
        score -= 1
    signals["score"] = score
    signals["max_score"] = 3
    return signals


def compare(by_arm: dict) -> dict:
    """Arm -> [answers]. Returns per-arm means and whether the arms separated at all.

    `separated` False is a real result and the one the first attempt got: it means the probe
    could not tell the arms apart, and nothing about the sentence follows from it either way.
    """
    out = {}
    for arm, answers in (by_arm or {}).items():
        graded = [grade(a) for a in answers]
        n = len(graded) or 1
        out[arm] = {
            "n": len(graded),
            "mean_score": sum(g["score"] for g in graded) / n,
            "cited_termination": sum(1 for g in graded if g["cites_termination"]),
            "split": sum(1 for g in graded if g["split_into_dated_ranges"]),
            "mentioned_skill": sum(1 for g in graded if g["mentions_skill_lookup"]),
            "graded": graded,
        }
    means = [v["mean_score"] for v in out.values()]

    # SEPARATION HAS TO CLEAR THE NOISE, and the first version of this did not ask it to.
    # `(max - min) > 0` calls any difference a separation, so three arms whose within-arm
    # scores ranged 0..2 were reported as separated on a gap of 0.33 -- and that was reported
    # upward as a finding before anyone looked at the spread. The threshold now is the widest
    # within-arm spread: a gap between arms means nothing until it exceeds the gap the SAME
    # arm produces against itself.
    #
    # This is a floor, not a test. With three runs per arm it is very easy to clear by luck
    # and very easy to miss a real effect; `underpowered` says so rather than leaving a reader
    # to infer it from n.
    spreads = [max(g["score"] for g in v["graded"]) - min(g["score"] for g in v["graded"])
               for v in out.values() if v["graded"]]
    within = max(spreads) if spreads else 0
    gap = (max(means) - min(means)) if means else 0
    separated = bool(means) and gap > within
    smallest_n = min((v["n"] for v in out.values()), default=0)

    return {
        "arms": out,
        "gap": round(gap, 3),
        "widest_within_arm_spread": within,
        "separated": separated,
        "underpowered": smallest_n < 10,
        # Stated so a reader cannot mistake a null for a negative: no separation means the
        # probe did not discriminate at this sample size, NOT that the sentence does nothing.
        "note": ("" if separated else
                 "the arms did not separate: the gap between arms (%.2f) does not exceed the "
                 "spread WITHIN an arm (%d), so this says the probe could not discriminate at "
                 "n=%d -- it is not evidence that the sentence has no effect"
                 % (gap, within, smallest_n)),
    }
