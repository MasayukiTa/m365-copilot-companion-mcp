"""Stage 0 of the settle-unification plan: replay recorded turns through two implementations.

WHAT THIS IS FOR

The plan proposes replacing the settle predicate and asks, sensibly, whether that is worth
doing before doing it. Its Stage 0 answers that offline: take the sample sequences a turn
actually went through, feed them to the old logic and the new one, and compare WHERE each
accepted. No fleet, no RAM, no live tenant, and nothing that touches Copilot's terms -- it is
re-analysis of logs already on disk.

WHY IT COULD NOT BE RUN AS WRITTEN

The plan names the settle trace as its input. The trace was persisting -- 5,731 lines, not
stderr -- but four things stood between it and a replay, and the first is the one that
matters:

  * it recorded only turns already past 60 seconds, and the primary endpoint is TRUNCATED
    CAPTURE, which is an early accept. Those turns settle in seconds and wrote nothing. The
    file was a record of the opposite population;
  * each line kept text_len and the last 90 characters, so a sequence cannot be reconstructed;
  * there was no turn identifier, so lines cannot be grouped into turns;
  * and no final text, so the ground-truth label the plan defines -- "the turn's last text" --
    cannot be built.

Collect mode in copilot_autopilot_relay fixes all four. This module is what consumes it.

THE LABEL, AND WHY IT NEARLY DEFINED THE ENDPOINT OUT OF EXISTENCE

`truncated` is the plan's definition: the implementation accepted where the text was shorter
than the turn's final text. Everything then depends on what "final" is, and the first version
of this got it exactly backwards.

`_settle_trace` is called from inside the settle loop, and the loop ENDS when production's
predicate accepts. So the recorded sequence stops at production's accept point, and the last
recorded text is production's accepted text. Both arms here are strictly weaker than
production -- production needs three stable samples (six without an end marker) AND dwell
time; the arms need two, or two plus a floor of three -- so both accept at an index at or
before production's. Scored against production's own accepted text, neither arm can be
truncated: the measurement was zero by construction, and the ~7% truncation the plan set out
to find was counted as zero PRECISELY on the turns where production truncated, because that
is the moment recording stopped.

Collect mode therefore keeps reading for a few polls after acceptance. Those samples are
marked `post_accept`, are never offered to either arm as a decision point, and exist only so
the ground truth is what the text actually settled to rather than what production decided it
had settled to. A turn recorded without that tail is reported as unlabelled rather than
scored, because a zero from it means nothing.

WHY THERE IS NO P-VALUE HERE

`accept_index_sampled` is `accept_index_legacy` plus a sample floor -- a strict superset of
conditions -- so the sampled arm can only ever accept at the same index or later. While text
grows monotonically it cannot be truncated where the legacy arm is not, and `sampled_worse`
is structurally zero. McNemar's null is that both discordant directions are equally likely,
which is not merely unlikely here but impossible, and the plan's "about forty discordant
pairs" figure was derived from that null. Testing it would report that an impossible thing
did not happen.

So the result is an ESTIMATE OF THE SIZE of the reduction with a Wilson interval, and no
significance claim. The direction is a property of the predicates and was never in question.
The sampled arm's one real cost -- turns where the floor holds out until production would have
timed out -- is reported separately as `never_only`, because a timeout is not a truncation and
adding them together would make two different problems into one number.

WHAT THESE TWO ARMS ARE, AND WHAT THEY ARE NOT

They are the two ACCEPTANCE RULES -- generation counting and repeated text -- replayed over
recorded samples. They are not production's settle predicate, which additionally varies its
sample count by marker, requires dwell time, skips processing placeholders, and refuses a
repeat it considers stale. A difference measured here is therefore a difference between the
rules, not a prediction of the difference between the two builds; the live A/B the plan
schedules afterwards is what answers that, and this stage exists to decide whether it is
worth running at all.

The simplification is deliberate rather than incidental. Replaying the full predicate would
mean reproducing its timing behaviour from a log that records no timing, which would put
invented dwell values inside the comparison -- and a paired test is only worth running when
both arms differ in the one thing being tested.
"""
from __future__ import annotations

import io
import json
import os
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TRACE = os.path.join(REPO, ".fleet", "settle_trace.jsonl")


class NotReplayable(RuntimeError):
    """Raised when a trace cannot answer the question, with what is missing."""


def load_turns(path=None) -> dict:
    """{turn_id: [poll, ...]} in recorded order, from a collect-mode trace.

    Refuses rather than degrading. A trace without turn ids or full text can be summarised
    into a number, and that number would describe something other than what the caller asked
    -- which is the failure this whole exercise exists to avoid.
    """
    path = path or DEFAULT_TRACE
    rows = []
    try:
        for line in io.open(path, encoding="utf-8", errors="replace"):
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except OSError as exc:
        raise NotReplayable("no trace at %s (%s)" % (path, exc))

    if not rows:
        raise NotReplayable(
            "the trace at %s is empty. An empty trace is a real finding in this module -- it "
            "is how a fifteen-minute hang was traced to a client bug -- but it cannot be "
            "replayed." % path)

    # EVERY ROW, not the first one. A trace whose format changed part way through -- a relay
    # restarted without collect mode, an older file appended to -- passes a check that looks
    # only at rows[0], and the rows that cannot be replayed are then silently replayed.
    bad = [i for i, r in enumerate(rows, 1)
           if "turn_id" not in r or "text" not in r]
    if bad:
        raise NotReplayable(
            "this trace is not fully collect-mode: %d of %d rows lack turn_id or text "
            "(first at line %d). Re-record with MCP_SETTLE_TRACE_COLLECT=1, which also drops "
            "the 60-second gate that excludes exactly the early-accept turns the primary "
            "endpoint is about." % (len(bad), len(rows), bad[0]))

    # AN UNLABELLED POLL IS NOT A TURN. Collapsing them under one key merges polls from
    # unrelated turns into a single enormous pseudo-turn whose "final text" belongs to
    # whichever turn happened to be last -- every label in it wrong, and nothing about the
    # result looking unusual.
    unlabelled = sum(1 for r in rows if not r.get("turn_id"))
    if unlabelled:
        raise NotReplayable(
            "%d of %d rows carry an empty turn_id. They cannot be grouped, and grouping them "
            "together would build one pseudo-turn out of unrelated polls."
            % (unlabelled, len(rows)))

    turns = defaultdict(list)
    for r in rows:
        turns[r["turn_id"]].append(r)
    return dict(turns)


# --------------------------------------------------------------------------------------
# The two implementations, as pure functions over a recorded sequence
# --------------------------------------------------------------------------------------

def accept_index_legacy(polls, *, need_stable=2) -> int:
    """Where the current predicate would have accepted, or -1.

    Modelled on wait_for_idle's rule as it stands: not generating, and the text unchanged for
    `need_stable` consecutive polls.
    """
    # RUN LENGTH, counting the first read. Two identical reads is a run of two, which is what
    # "unchanged for two polls" means to everyone who says it -- counting transitions instead
    # is off by one and silently makes the predicate stricter than the thing it models.
    run = 0
    previous = None
    for i, p in enumerate(polls):
        text = p.get("text", "")
        if p.get("generating"):
            run, previous = 0, None
            continue
        run = run + 1 if text == previous else 1
        previous = text
        if run >= need_stable and text.strip():
            return i
    return -1


def accept_index_sampled(polls, *, need_stable=2, need_samples=3) -> int:
    """Where the proposed predicate would have accepted, or -1.

    The proposal adds a sample-count floor: stability is not enough on its own, because two
    identical reads can both land inside one pause in a stream that has not finished. The
    plan is explicit that this trades latency for correctness, and that the sample count is
    the quality argument and must not be the thing tuned away when the latency shows up.
    """
    run = 0
    previous = None
    for i, p in enumerate(polls):
        text = p.get("text", "")
        if p.get("generating"):
            run, previous = 0, None
            continue
        run = run + 1 if text == previous else 1
        previous = text
        if run >= need_stable and i + 1 >= need_samples and text.strip():
            return i
    return -1


IMPLEMENTATIONS = {"legacy": accept_index_legacy, "sampled": accept_index_sampled}


# --------------------------------------------------------------------------------------
# The comparison
# --------------------------------------------------------------------------------------

def _split_label(polls):
    """(polls the arms may decide on, the ground-truth final text).

    THE LABEL COMES FROM THE POST-ACCEPT TAIL WHEN THERE IS ONE. `_settle_trace` runs inside
    the settle loop, and the loop ends when PRODUCTION accepts -- so without the tail the last
    recorded text is production's accepted text, every arm is weaker than production and
    therefore accepts no later than it, and truncation measured against that label is zero by
    construction. The turns that truncate are exactly the ones where recording stopped early,
    so the endpoint counted them as clean.

    The tail samples are excluded from what the arms see. They are label evidence, not
    decision evidence; letting an arm accept on a sample recorded after production had already
    returned would measure a predicate nobody could run.
    """
    decidable = [p for p in polls if not p.get("post_accept")]
    tail = [p for p in polls if p.get("post_accept")]
    final = (tail[-1].get("text") if tail else
             (decidable[-1].get("text") if decidable else "")) or ""
    return decidable, final, bool(tail)


def replay(turns, implementations=None) -> dict:
    """Run every recorded turn through each implementation and count truncated captures.

    Returns per-implementation counts and the PAIRED outcome, which is the whole point of
    recording sequences rather than rates.
    """
    impls = implementations or IMPLEMENTATIONS
    per = {name: {"accepted": 0, "never": 0, "truncated": 0} for name in impls}
    discordant = {"legacy_worse": 0, "sampled_worse": 0}
    never_only = {name: 0 for name in impls}
    rows = []
    unlabelled_turns = 0

    for turn_id, polls in sorted(turns.items()):
        decidable, final, labelled = _split_label(polls)
        if not labelled:
            unlabelled_turns += 1
        outcome = {}
        for name, fn in impls.items():
            idx = fn(decidable)
            if idx < 0:
                per[name]["never"] += 1
                outcome[name] = None
                continue
            per[name]["accepted"] += 1
            accepted = decidable[idx].get("text") or ""
            truncated = accepted != final
            per[name]["truncated"] += bool(truncated)
            outcome[name] = {"index": idx, "truncated": truncated,
                             "accepted_len": len(accepted), "final_len": len(final)}

        a, b = outcome.get("legacy"), outcome.get("sampled")
        if a and b:
            if a["truncated"] != b["truncated"]:
                discordant["legacy_worse" if a["truncated"] else "sampled_worse"] += 1
        else:
            # NOT DROPPED. A turn one arm accepted and the other never did was previously
            # excluded from the comparison entirely -- and "never accepted" is the sampled
            # arm's ONLY failure mode, the sample floor holding out until production would
            # have timed out. Excluding one arm's single way of failing from the paired
            # summary is not a neutral simplification.
            for name in impls:
                if outcome.get(name) is None and any(
                        outcome.get(other) is not None for other in impls if other != name):
                    never_only[name] += 1

        rows.append({"turn_id": turn_id, "polls": len(decidable),
                     "labelled": labelled, **outcome})

    total = len(turns)
    for name, counts in per.items():
        counts["truncation_rate"] = (counts["truncated"] / counts["accepted"]
                                     if counts["accepted"] else None)

    return {
        "turns": total,
        "unlabelled_turns": unlabelled_turns,
        "per_implementation": per,
        "discordant": discordant,
        "discordant_total": sum(discordant.values()),
        # WHAT ONE ARM FAILED AT AND THE OTHER DID NOT, kept beside the truncation figures
        # rather than folded into them: never-accepting is a latency cost, not a truncation,
        # and adding it to the same column would make two different problems one number.
        "never_only": never_only,
        "reduction": reduction(per, total),
        "rows": rows,
    }


def reduction(per, turns) -> dict:
    """How much smaller the truncation rate got, with an interval -- NOT a McNemar p-value.

    THE PLAN ASKED FOR THE WRONG TEST AND THIS IS WHERE THAT SHOWS. McNemar's null hypothesis
    is that the two discordant directions are equally likely, and the ~40-pair power
    calculation is derived from it. But `accept_index_sampled`'s condition is
    `accept_index_legacy`'s condition PLUS a sample floor -- a strict superset -- so the
    sampled arm always accepts at an index at or after the legacy arm's. While the text grows
    monotonically, the sampled arm cannot be truncated where the legacy arm is not, and
    `sampled_worse` is structurally zero. A p-value against a null that cannot happen reports
    that an impossible thing did not occur.

    What is actually wanted is the SIZE of the reduction, since its direction is a property of
    the predicates rather than something to be discovered. So: the difference in truncation
    counts over the turns, with a Wilson interval, and no significance claim at all.
    """
    legacy = per.get("legacy") or {}
    sampled = per.get("sampled") or {}
    fixed = max(0, legacy.get("truncated", 0) - sampled.get("truncated", 0))
    n = max(1, turns)
    point = fixed / n
    low, high = _wilson(fixed, n)
    return {"turns": n, "turns_fixed": fixed, "point": round(point, 4),
            "ci95": [round(low, 4), round(high, 4)],
            "note": "the direction is structural -- the sampled predicate is the legacy one "
                    "plus a sample floor, so it can only accept later. The estimate is of "
                    "the SIZE of the reduction; there is no hypothesis test here because "
                    "there is no null that could be true."}


def _wilson(successes, n, z=1.96):
    """Wilson score interval. Behaves at 0 and at n, where the normal approximation does not."""
    if n <= 0:
        return 0.0, 0.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return max(0.0, centre - half), min(1.0, centre + half)


def report(result) -> str:
    lines = ["settle replay: %d turns" % result["turns"]]
    for name, c in sorted(result["per_implementation"].items()):
        rate = c["truncation_rate"]
        lines.append("  %-8s accepted %4d  never %3d  truncated %4d  rate %s"
                     % (name, c["accepted"], c["never"], c["truncated"],
                        "%.3f" % rate if rate is not None else "n/a"))

    r = result["reduction"]
    lines.append("  reduction:  %d of %d turns no longer truncated  =  %.3f  95%% CI "
                 "[%.3f, %.3f]" % (r["turns_fixed"], r["turns"], r["point"],
                                   r["ci95"][0], r["ci95"][1]))

    never_only = result.get("never_only") or {}
    for name, count in sorted(never_only.items()):
        if count:
            lines.append("  COST: %s never accepted on %d turn(s) the other arm accepted -- "
                         "that is a timeout in production, not a truncation, and it is the "
                         "sample floor's only failure mode" % (name, count))

    d = result["discordant"]
    lines.append("  discordant: legacy worse %d / sampled worse %d"
                 % (d["legacy_worse"], d["sampled_worse"]))
    if d["sampled_worse"] == 0:
        lines.append("  (sampled_worse is structurally zero: the sampled predicate is the "
                     "legacy one plus a floor, so it cannot accept earlier. This is not "
                     "evidence -- it is the definition.)")

    if result.get("unlabelled_turns"):
        lines.append("  WARNING: %d turn(s) have no post-accept tail, so their label is "
                     "production's accepted text and their truncation count is zero by "
                     "construction. Re-record with collect mode's label tail."
                     % result["unlabelled_turns"])
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="relay.settle_replay", description=__doc__.splitlines()[0])
    ap.add_argument("--trace", default=DEFAULT_TRACE)
    args = ap.parse_args(argv)
    try:
        turns = load_turns(args.trace)
    except NotReplayable as exc:
        print("cannot replay: %s" % exc)
        return 2
    print(report(replay(turns)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
