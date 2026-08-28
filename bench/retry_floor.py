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


def report(path, max_k=5, min_attempts=2):
    rows = load_history(path)
    grouped = group_attempts(rows, min_attempts)
    curve = floor_curve(grouped, max_k)
    return {
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
        "not_an_accuracy_floor": (
            "DONE is the worker reporting that it finished; nothing external checked the "
            "answer. A mechanism asked to beat these rates is being asked to beat a claim, "
            "not a result. An oracle is required before any of this is an accuracy floor."),
        "read_this_first": (
            "k=1 here is not the general single-attempt rate: this population is goals that "
            "were retried, and a goal is retried because its first attempt failed. Compare "
            "the MARGINAL column, which holds the same goals fixed across k."),
    }
