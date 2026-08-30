"""What a bare retry already achieves, measured from runs that already happened.

WHY THIS IS THE FIRST NUMBER. Every mechanism proposed on top of a single attempt -- a refuter
panel, best-of-N, a research budget -- has to beat simply running the goal again. `AI Agents
That Matter` names the failure of not asking: an elaborate scaffold at 88.0% and $134.50
against a plain retry at 92.0% and $2.51. Without this floor, "the panel reached 0.8" is a
number with nothing under it.

NO NEW RUNS WERE NEEDED. The fleet ledger already holds goals attempted more than once, which
is exactly the experiment -- it was simply never read as one.

THE POPULATION TRAP THIS FILE EXISTS TO AVOID. The first version of this calculation reported
"k=3: 1.010 (98/97)" and "k=5: 4.167 (100/24)" -- rates above 1, which is impossible and was
visible only because the counts were printed beside the ratio. The numerator counted goals
whose first success came within k across EVERY goal, while the denominator counted only goals
that had k attempts to give. Two populations, one fraction. Every k-of-n figure below is
therefore computed over goals with AT LEAST k attempts, and the eligible count is returned
next to the rate so the same mistake cannot hide again.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict


def load_history(path):
    """The fleet ledger, or an empty list. Never raises."""
    try:
        with open(path, encoding="utf-8") as fh:
            rows = json.load(fh)
    except (OSError, ValueError):
        return []
    return rows if isinstance(rows, list) else []


def group_attempts(rows, min_attempts=2):
    """goal -> attempts in time order, for goals attempted at least `min_attempts` times."""
    by_goal = defaultdict(list)
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        g = (r.get("goal") or "").strip()
        if g:
            by_goal[g].append(r)
    out = {}
    for g, rs in by_goal.items():
        if len(rs) >= min_attempts:
            out[g] = sorted(rs, key=lambda r: r.get("ts") or 0)
    return out


def _succeeded(rec):
    """DONE, WHICH IS NOT THE SAME AS CORRECT -- and this file was read as though it were.

    DONE is the fleet's terminal state for a worker that reported finishing. Nothing external
    checked the answer. The curve below is therefore a curve of COMPLETION, and the figure
    "k=2 reaches 0.931" was reported as a floor of accuracy that a panel or best-of-N would
    have to beat. It is not: a worker can report DONE on a wrong answer, and on this evidence
    we cannot tell how often it does.

    The distinction is the same one this project already holds elsewhere -- a self-report
    nobody verifies is worth less than no field at all -- applied to the loop's own number.

    To turn this into an accuracy floor, the same goals need an ORACLE: the answer re-derived
    from the source, an independent recomputation, a read-after-write, an executed test. Until
    then every rate here reads "the worker said it was done", and the mechanisms it is meant
    to benchmark are being asked to beat a claim rather than a result.
    """
    return (rec.get("outcome") or "").upper() == "DONE"


# ---------------------------------------------------------------------------------------
# THE ORACLE THE FILE HAS BEEN ASKING FOR SINCE IT WAS WRITTEN
#
# _succeeded() above is the fleet saying it finished. A graded slice is the first external
# check this project has had, and two independent reviews of the mechanism-usage data reached
# the same verdict: everything in this system was accepted against that self-report, whose
# measured precision is 71.8% -- 28 right, 11 wrong out of 39 DONE claims.
#
# So the curve is computed TWICE and both are reported. The gap between them is not noise;
# it is the false-DONE tax, and it is the number that says how much of the loop's own
# reasoning was built on a claim rather than a result.
#
# An attempt whose instance is not in the graded slice is UNKNOWN, not failed. Counting it as
# failed would let the size of the grading effort move the floor, which is the population trap
# this file already carries a warning about.
_UNKNOWN = object()


def _graded(rec, verdicts, wt_map):
    """True / False / _UNKNOWN for one attempt, from the grader rather than the worker.

    Joined on the checkout the goal names, because goals do not carry the instance id -- a
    join on the id matched nothing at all the first time it was tried, and 40 instances came
    back as "nobody ever claimed anything".
    """
    if not verdicts or not wt_map:
        return _UNKNOWN
    goal = (rec.get("goal") or "").replace("/", "\\").lower()
    hits = [inst for inst, path in wt_map.items()
            if str(path).replace("/", "\\").lower() in goal]
    if len(hits) != 1:
        return _UNKNOWN
    if hits[0] not in verdicts:
        return _UNKNOWN
    return bool(verdicts[hits[0]])


def graded_curve(attempts_by_goal, verdicts, wt_map, max_k=5):
    """The same shape as floor_curve, but over correctness -- OR a refusal, and usually that.

    THE REFUSAL IS THE POINT. One patch is captured per instance, so every attempt of a goal
    joins to the SAME verdict. A curve computed over that is flat by construction: measured on
    the first graded slice, k=1 and k=2 both came out at 0.755 with an identical numerator and
    denominator, which looks like "retry adds nothing" and actually means "the question was
    never asked".

    An external review named this before it was run: with only the final artifact graded, you
    cannot tell whether the second attempt rescued a failure, preserved a success, or replaced
    a success with a failure. Reporting a number here would answer a different question in the
    shape of this one -- the exact error that made `outcome == DONE` acceptable for two years.

    So: if every attempt of every goal resolves to one verdict, this returns why it cannot
    answer rather than a curve. Grading per-attempt snapshots is what unlocks it.
    """
    # Does any goal have attempts that could be told apart at all?
    distinguishable = 0
    for rs in attempts_by_goal.values():
        seen = {id(_graded(r, verdicts, wt_map)) if _graded(r, verdicts, wt_map) is _UNKNOWN
                else _graded(r, verdicts, wt_map) for r in rs}
        if len({v for v in seen if v is not _UNKNOWN}) > 1:
            distinguishable += 1
    if distinguishable == 0:
        return {
            "refused": True,
            "reason": ("one patch is captured per instance, so every attempt of a goal joins "
                       "to the same verdict. A per-k curve over that is flat by construction "
                       "and would read as 'retry adds nothing'."),
            "what_would_unlock_it": ("capture and grade a patch per ATTEMPT, then compute "
                                     "rescue P(G2=1,G1=0) and regression P(G2=0,G1=1)"),
            "goals_with_distinguishable_attempts": 0,
        }
    curve = []
    for k in range(1, max_k + 1):
        eligible = [rs for rs in attempts_by_goal.values() if len(rs) >= k]
        if not eligible:
            continue
        known, solved = 0, 0
        for rs in eligible:
            vals = [_graded(r, verdicts, wt_map) for r in rs[:k]]
            if all(v is _UNKNOWN for v in vals):
                continue
            known += 1
            if any(v is True for v in vals):
                solved += 1
        curve.append({
            "k": k,
            "goals_with_k_attempts": len(eligible),
            # STATED, NOT HIDDEN: how many of those could be graded at all.
            "gradable": known,
            "ungradable": len(eligible) - known,
            "solved_within_k": solved,
            "rate": (solved / known) if known else None,
        })
    return curve


def floor_curve(attempts_by_goal, max_k=5):
    """P(at least one success within k attempts), for each k, over a HONEST denominator.

    A goal contributes to k only if it actually had k attempts. That is what makes the curve
    a measurement rather than an artefact of which goals happened to be retried often.
    """
    curve = []
    for k in range(1, max_k + 1):
        eligible = [rs for rs in attempts_by_goal.values() if len(rs) >= k]
        if not eligible:
            continue
        solved = sum(1 for rs in eligible if any(_succeeded(r) for r in rs[:k]))
        curve.append({
            "k": k,
            "eligible": len(eligible),
            "solved": solved,
            "rate": solved / len(eligible),
            # The extra goals solved by allowing one MORE attempt than k-1. This is the number
            # a mechanism has to beat, not the level itself.
            "marginal": None,
        })
    for i in range(1, len(curve)):
        # Marginal gain is only meaningful between two k computed on the SAME goals, so it is
        # recomputed on the intersection rather than subtracted across differing denominators.
        k = curve[i]["k"]
        common = [rs for rs in attempts_by_goal.values() if len(rs) >= k]
        prev = sum(1 for rs in common if any(_succeeded(r) for r in rs[:k - 1]))
        now = sum(1 for rs in common if any(_succeeded(r) for r in rs[:k]))
        curve[i]["marginal"] = (now - prev) / len(common) if common else None
    return curve


def per_attempt_rate(attempts_by_goal):
    """The plain single-attempt success rate over every attempt of every retried goal.

    NOT the same as the k=1 point of the curve. This averages over attempts; the curve's k=1
    averages over goals, counting each goal's FIRST attempt only. Reporting one as the other
    is how a retried-heavy population inflates an apparent single-shot rate.
    """
    total = sum(len(rs) for rs in attempts_by_goal.values())
    if not total:
        return None
    done = sum(1 for rs in attempts_by_goal.values() for r in rs if _succeeded(r))
    return {"attempts": total, "succeeded": done, "rate": done / total}


def report(path, max_k=5, min_attempts=2, eval_path=None, slice_path=None):
    rows = load_history(path)
    grouped = group_attempts(rows, min_attempts)
    curve = floor_curve(grouped, max_k)

    # THE SECOND FLOOR, when a grader is available. Not a replacement for the first: the two
    # are reported side by side and their gap is the false-DONE tax.
    graded, verdicts, wt_map = None, None, None
    if eval_path and slice_path and os.path.exists(eval_path) and os.path.exists(slice_path):
        verdicts = {k: bool(v) for k, v in
                    json.load(open(eval_path, encoding="utf-8-sig")).items()}
        ids = sorted(r["instance_id"] for r in
                     json.load(open(slice_path, encoding="utf-8-sig")))
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            ".fleet", "swe", "work")
        wt_map = {inst: os.path.join(root, "p%02d" % i) for i, inst in enumerate(ids)}
        graded = graded_curve(grouped, verdicts, wt_map, max_k)

    return {
        "graded_curve": graded,
        "graded_note": (
            "correctness from the grader, not the worker's DONE. None when no graded slice "
            "was supplied. Compare k-for-k against `curve`: the gap is how much of the "
            "completion floor was a claim rather than a result."
            if graded is None else
            "the floor a mechanism actually has to beat. `curve` is the same measurement "
            "against self-reported DONE and is kept only so the two can be compared."),
        # NAMED FOR WHAT IT COUNTS. It was "curve" and was read as accuracy.
        "measures": "completion (outcome == DONE), NOT external correctness",
        "ledger_rows": len(rows),
        "goals_retried": len(grouped),
        "per_attempt": per_attempt_rate(grouped),
        "curve": curve,
        # THE CONFOUND, RETURNED WITH THE NUMBERS SO IT CANNOT BE DROPPED IN THE RETELLING.
        #
        # This population is "goals somebody retried", and goals get retried BECAUSE the first
        # attempt failed. So the k=1 point is conditioned on early failure and is not the
        # general single-attempt rate -- it is the single-attempt rate among goals selected
        # for having gone badly. Measured here: k=1 = 0.178 against an all-attempts rate of
        # 0.667 on the same goals, and the gap is the selection, not a finding.
        #
        # The MARGINAL gains from k=2 onward are far less exposed to it: those compare the
        # same goals against themselves with one more attempt, inside a population already
        # conditioned the same way for every k.
        "k1_is_selection_biased": True,
        # AND THE BIAS RUNS THE OTHER WAY FROM THE ONE ASSUMED HERE.
        #
        # Both reviews of this data made the same correction: the retried group is the one
        # whose first attempt was DETECTED as failed, which is the disadvantaged population --
        # and it still reaches the higher rate. The selection understates retry rather than
        # inflating it.
        #
        # The sharper reading is about the group that is NOT retried. A goal stops at one
        # attempt when the worker said DONE and nothing checked, so the single-attempt bucket
        # is where false-DONE settles: measured precision of DONE is 71.8%, and the
        # single-attempt graded rate is 42.9%. Retry works, and its trigger is wired to the
        # self-report, so it never fires on the population that most needs it.
        "the_unretried_group_is_where_false_done_settles": True,
        "not_an_accuracy_floor": (
            "DONE is the worker reporting that it finished; nothing external checked the "
            "answer. A mechanism asked to beat these rates is being asked to beat a claim, "
            "not a result. An oracle is required before any of this is an accuracy floor."),
        "read_this_first": (
            "k=1 here is not the general single-attempt rate: this population is goals that "
            "were retried, and a goal is retried because its first attempt failed. Compare "
            "the MARGINAL column, which holds the same goals fixed across k."),
    }
