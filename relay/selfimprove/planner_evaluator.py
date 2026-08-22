"""The second instrument: turns, for the `planner` coordinate.

WHY A SECOND INSTRUMENT AND NOT A WIDER FIRST ONE

`route_evaluator` measures the commit charge a run creates in Edge. That responds to how many
renderers the fleet opens and to nothing else, so it cannot see a planner change however long
it runs -- twenty minutes to return INCONCLUSIVE for a structural reason. The instrument is not
broken and does not want widening; it wants a sibling.

What IS reused is the recipe, because that is where the day's lessons live:

    null run first          two identical arms, to find out how far apart they land
    threshold after         set from that spread, never from an observed effect
    declare the range       MEASURES, so a comparison can refuse before it spends the time
    both arm orders         a sign that does not survive the swap is not a sign
    an arm that broke       INFRA, never a verdict

THE THRESHOLD WAS UNSET UNTIL IT WAS MEASURED

`MIN_TURNS_GAIN` began as None with `decide()` raising, because the memory floor had spent a
day as a number borrowed from an unrelated constant and only meant something once a null run
gave it a spread to sit above. It is 0.75 now, and the derivation is beside the constant --
including why the two dedicated null passes reporting 0.000 could not be taken at face value.

WHY TURNS

`worker_done.turns` already carries a count per goal, so the observable exists without
instrumenting anything -- which is what made `planner` the cheapest of the six coordinates
audited.

THE MECHANISM I PREDICTED DID NOT HAPPEN, AND THAT IS THE FIRST THING TO SAY

The argument for pointing turns at `planner` was that a version which plans first spends a
turn planning. Measured, it does not: `planner/v2`'s opening body is 257 characters longer
than `planner/v1`'s, and the model plans and proceeds inside that same first turn. A clean
comparison -- both arms logging four goals, four turns each -- put the difference at exactly
0.00 turns per goal.

`route_evaluator`'s own MEASURES comment says a component whose effect is argued rather than
demonstrated should stay out until a null run says otherwise. I wrote that rule and then broke
it here, one file later, on the strength of a docstring that says "plan first".

WHAT THAT LEAVES. One clean comparison in ONE arm order says v1 and v2 cost the same turns on
these four goals. That is a real result about this pair on this workload, and it is NOT yet a
demonstration that turns can see planner in general -- an instrument that has only ever
reported zero for its one coordinate has not shown it can report anything else. MEASURES keeps
`planner` because the coordinate reaches the fleet and the observable is real, and the claim
is downgraded here from mechanism to open question rather than quietly left standing.
"""
from __future__ import annotations

import json
import os

#: The components this instrument has a mechanism to see. Declared here for the same reason
#: route_evaluator declares its own: the range is a property of the measurement, and a caller
#: holding the list describes whichever instrument was written first.
MEASURES = ("planner",)

MEASURES_NOTE = ("turns per goal, which moves with how much of the work a harness does before "
                 "it starts")

#: How many turns per goal the candidate must save before "better" is claimed.
#:
#: MEASURED, FROM 22 ARMS THAT WERE ALL THE SAME PROGRAM.
#:
#: Two dedicated null passes both reported a spread of 0.000, and taking that at face value
#: would have been the mistake: on those eight goals every one finished in a single turn, so
#: the instrument had no room to vary and 0.000 was its resolution rather than its noise. The
#: answer came from the log instead. Across all 89 `worker_done` rows recorded today -- 22
#: four-goal arms, none of them a treatment against another -- turns per goal was 1.0 in 18
#: arms, 1.25 in two, and 1.5 in one:
#:
#:     goals at 2 turns          4 of 89   (4.5%)
#:     arm-to-arm difference     0 in 74% of the 231 pairs, 0.25 or 0.5 in the rest
#:     largest pair difference   0.500
#:
#: So 0.25 -- the finest difference four goals can express -- sits INSIDE the noise: identical
#: arms reach it routinely. 0.5 is the largest gap two same-program arms produced, and the
#: threshold has to clear that rather than sit on it, which is why this is 0.75 and not 0.5.
#:
#: THE EFFECT THIS WAS SIZED AGAINST TURNED OUT NOT TO EXIST. The floor was chosen expecting
#: `planner/v2` to move turns by about 1.0 by spending a turn planning. It does not -- both
#: arms measured 1.000 turns per goal in both orders. The floor is still the right floor for
#: the quantity; what was wrong was the prediction it was sized against, and the module
#: docstring says so.
#:
#: REVISIT IF THE GOALS CHANGE. This floor is a property of THESE goals: four of them, mostly
#: one-turn. A set where goals routinely take three or four turns has a different spread, and
#: this number would then be describing a workload that no longer exists.
MIN_TURNS_GAIN = 0.75

#: What the floor above was derived from, so a later reader can check it rather than trust it.
NULL_SPREAD_OBSERVED = {"arms": 22, "goals": 89, "max_pair_difference": 0.5,
                        "pairs_nonzero": 0.26, "goals_over_one_turn": 4}

#: Free physical memory an arm needs. Same quantity and same operator setting as the route
#: evaluator's -- a fleet that swaps does not run the thing you think it does, whatever is
#: being measured.
MIN_FREE_MB = 512.0


class NotCalibrated(RuntimeError):
    """A verdict was asked for before the instrument had a measured noise floor."""


def preflight(*, free_mb, calibrated=None, observable_recorded=True,
              improvement_detectable=None) -> list:
    """Reasons this comparison must not run. Empty means it may.

    `observable_recorded` is the one this instrument needs that the memory one does not.
    `worker_done` rows -- where turns live -- are written by `socket_route.record`, which is a
    NO-OP while the route is disabled. A run with both arms on tabs therefore produces a
    complete, ordinary-looking result with zero rows to count, and the only way to find out is
    to wait for it. Learned by waiting thirty minutes for exactly that, after writing the
    warning down in the coordinate audit and then launching the run anyway.
    """
    reasons = []
    # SAID BEFORE THE RUN, NOT AFTER IT. A saturated workload does not make a comparison
    # invalid -- "it did no harm" is a real claim -- but it makes one half of the possible
    # answers unreachable, and an operator deciding whether to spend twenty minutes should
    # know that the run cannot come back saying the candidate helped.
    if improvement_detectable is False:
        reasons.append(
            "the goal set is saturated: nearly every goal already finishes in one turn and "
            "reaches DONE, and neither can improve past that. This comparison can only detect "
            "harm. Run it deliberately for that, or pick goals with room before spending the "
            "time -- but pick them BEFORE seeing a result, or the workload is being chosen to "
            "fit the answer.")
    if not observable_recorded:
        reasons.append(
            "turns are read from `worker_done` rows, and those are written only while the "
            "socket route is enabled -- a run with both arms on tabs records nothing to count "
            "and looks entirely normal for the twenty minutes it takes to find out. Enable the "
            "route on both arms, or measure a different observable.")
    if free_mb is not None and free_mb < MIN_FREE_MB:
        reasons.append("%.0f MB free, floor %.0f (the operator's setting for this machine)"
                       % (free_mb, MIN_FREE_MB))
    if (MIN_TURNS_GAIN if calibrated is None else calibrated) is None:
        reasons.append(
            "this instrument has no measured noise floor yet. Run the null pass -- both arms "
            "the same harness -- record how far apart two identical arms land on turns, and "
            "set MIN_TURNS_GAIN above that. A threshold chosen any other way is a number "
            "wearing a calibration's clothes.")
    return reasons


def turns_from_log(path, since_ts=0.0, route=None) -> dict:
    """{"goals": n, "turns": total, "done": n_done} from `worker_done` rows after `since_ts`.

    READS THE LOG THE FLEET ALREADY WRITES. `worker_done` carries turns, outcome and status per
    goal, so the observable exists without instrumenting anything -- which is what made this
    coordinate the cheapest of the six audited.

    `route` narrows to rows whose transport matches, for a comparison where that is held fixed.
    """
    goals = turns = done = 0
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("event") != "worker_done":
                    continue
                if float(row.get("ts", 0) or 0) < since_ts:
                    continue
                if route is not None and row.get("route") != route:
                    continue
                goals += 1
                turns += int(row.get("turns", 0) or 0)
                if str(row.get("outcome", "")).upper() == "DONE":
                    done += 1
    except Exception:
        pass
    return {"goals": goals, "turns": turns, "done": done}


def turns_per_goal(arm) -> float | None:
    """The arm's turns per goal, or None when nothing was counted to divide by.

    DIVIDED BY WHAT WAS COUNTED, NOT BY WHAT WAS SENT. An arm carries both `goals` -- how many
    it was given -- and `logged_goals`, how many left a `worker_done` row for the turns to be
    read from. Those differ whenever a goal produced no row, and dividing the counted turns by
    the sent count then reports a smaller average for a reason that has nothing to do with the
    harness: four turns over four goals is 1.0, and the same four turns over three logged goals
    is 1.33, and only one of those is a statement about how the harness works.

    NOTHING LOGGED IS NOT ZERO TURNS. The first version fell back to `goals` when
    `logged_goals` was 0, and an arm that recorded nothing at all became "0 turns over 4
    goals = 0.0 turns per goal" -- a fabricated measurement, produced by the very change whose
    comment was about dividing by what was counted. It cost a real run: the control arm logged
    nothing, the judge read 0.0 against the candidate's 1.0, and reported a difference of
    -1.00 that no measurement supports.

    An arm with no rows returns None, and the caller has to decide what to do about that
    rather than being handed a number.
    """
    arm = arm or {}
    if "logged_goals" in arm:
        counted = int(arm.get("logged_goals") or 0)
    else:
        counted = int(arm.get("goals", 0) or 0)
    if counted <= 0:
        return None
    return float(arm.get("turns", 0) or 0) / counted


def decide(control, candidate, *, min_gain=None, per_class=None) -> dict:
    """The verdict from two arms. Pure -- no clock, no fleet, no browser.

    Raises `NotCalibrated` when no threshold has been measured, because returning
    INCONCLUSIVE there would be indistinguishable from "we measured and found nothing", and
    those are the two claims this whole apparatus exists to keep apart.
    """
    floor = MIN_TURNS_GAIN if min_gain is None else min_gain
    if floor is None:
        raise NotCalibrated(
            "no measured noise floor for turns. A verdict now would be a guess wearing the "
            "shape of a measurement; run the null pass first.")

    # The route rule, unchanged and not restated: an arm whose route closed mid-run is not the
    # arm the row claims it is, whatever quantity is being measured.
    from relay.selfimprove import route_evaluator as RV
    route = RV.fallback_verdict(control, candidate)
    if route.get("aborted"):
        return {"verdict": "inconclusive", "aborted": True, "turns_gain": None,
                "why": route["why"]}

    done_c = int((control or {}).get("done", 0))
    done_p = int((candidate or {}).get("done", 0))
    tpg_c, tpg_p = turns_per_goal(control), turns_per_goal(candidate)
    if tpg_c is None or tpg_p is None:
        return {"verdict": "inconclusive", "aborted": True, "turns_gain": None,
                "why": "an arm completed no goals, so it has no turns-per-goal to compare"}

    gain = tpg_c - tpg_p
    if done_p < done_c:
        return {"verdict": "reject", "turns_gain": round(gain, 3),
                "why": "completion fell: %d against the control's %d. Spending fewer turns by "
                       "finishing less is not an improvement, whatever the average says."
                       % (done_p, done_c)}
    if gain >= floor:
        return {"verdict": "keep", "turns_gain": round(gain, 3),
                "why": "completion held at %d and turns per goal fell by %.2f (floor %.2f)."
                       % (done_p, gain, floor)}
    # A LARGE NEGATIVE GAIN IS A FINDING, NOT A SHRUG.
    #
    # The first version merged this with the null case and told the operator the number was
    # "under the floor" -- of -1.00 against a floor of 0.75, which is 1.33 times it in the
    # other direction. Detecting that the candidate costs a full extra turn per goal is
    # exactly what this instrument was built for, and reporting it as "we could not tell"
    # throws away the one clear result the day produced. The same wording sat in the route
    # evaluator and had already misdescribed a -666 MB run as inconclusive.
    if gain <= -floor:
        return {"verdict": "reject", "turns_gain": round(gain, 3),
                "why": "completion held at %d and turns per goal ROSE by %.2f, past the %.2f "
                       "this instrument can distinguish from noise. That is a measured cost, "
                       "not an absence of evidence." % (done_p, -gain, floor)}
    # BEFORE CALLING IT A NULL, ASK THE CLASSES. An aggregate of zero is produced both by a
    # harness that changed nothing and by one that helped one class and hurt another by the
    # same amount, and only the first of those is "no difference". Skipped when the caller
    # supplied no breakdown -- a comparison that cannot split its goals still gets a verdict,
    # it just gets one with less behind it.
    split = None
    if per_class:
        split = classes_disagree(per_class.get("control") or {},
                                 per_class.get("candidate") or {}, floor)
        if split["disagree"]:
            return {"verdict": "inconclusive", "aborted": True,
                    "turns_gain": round(gain, 3), "per_class": split["per_class"],
                    "why": "the aggregate moved %.2f, but %s That is not a null result; it is "
                           "two findings that cancelled." % (gain, split["why"])}
    return {"verdict": "inconclusive", "turns_gain": round(gain, 3),
            "per_class": split["per_class"] if split else None,
            "why": "completion held at %d and turns per goal moved %.2f, inside the %.2f this "
                   "instrument can distinguish from noise. That is not a finding that either "
                   "harness is worse." % (done_p, gain, floor)}


# ------------------------------------------------------------------------------------------
# Per-class breakdown
# ------------------------------------------------------------------------------------------
#
# NOT SIMPSON'S PARADOX, AND CALLING IT THAT WOULD BE BORROWING AUTHORITY.
#
# Simpson's needs unequal group sizes between the arms, and both arms here run the SAME goals,
# so the mix is identical by construction and the classic reversal cannot occur. What CAN occur
# is plainer and just as bad: a candidate that helps one class and hurts another by the same
# amount averages to zero, and the run reports "no difference" about a harness that changed two
# things in opposite directions.
#
# The classes are not a guess. A goal in this campaign either carries machine-checkable
# acceptance criteria or it does not, which is a structural property of the goal rather than a
# predicate somebody invented -- and it happens to separate the two kinds of work the set
# contains: local artefacts that can be verified, and Work IQ answers that cannot.

VERIFIED, UNVERIFIED = "verified", "unverified"


def class_of(goal_text, goals) -> str:
    """Which class a logged goal belongs to, by exact match against the campaign's goals.

    Exact text, not a heuristic: the row's goal string came from the same list, so matching it
    is a lookup. A row that matches nothing returns UNVERIFIED rather than raising -- an
    unrecognised goal is unverified as far as anything here can tell.
    """
    from relay.relay_fleet import goal_fields
    text = (goal_text or "").strip()
    for goal in goals or []:
        gt, checks, _cwd = goal_fields(goal)
        if gt.strip()[:200] == text[:200]:
            return VERIFIED if checks else UNVERIFIED
    return UNVERIFIED


def turns_by_class(path, goals, since_ts=0.0) -> dict:
    """{class: {"goals": n, "turns": t, "done": d}} for `worker_done` rows after `since_ts`."""
    import json as _json
    out = {VERIFIED: {"goals": 0, "turns": 0, "done": 0},
           UNVERIFIED: {"goals": 0, "turns": 0, "done": 0}}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = _json.loads(line)
                except Exception:
                    continue
                if row.get("event") != "worker_done":
                    continue
                if float(row.get("ts", 0) or 0) < since_ts:
                    continue
                bucket = out[class_of(row.get("goal"), goals)]
                bucket["goals"] += 1
                bucket["turns"] += int(row.get("turns", 0) or 0)
                if str(row.get("outcome", "")).upper() == "DONE":
                    bucket["done"] += 1
    except Exception:
        pass
    return out


def classes_disagree(control_by_class, candidate_by_class, floor=None) -> dict:
    """Do the classes move in opposite directions by enough to matter?

    Returns {"disagree": bool, "per_class": {...}, "why": str}. The aggregate is not recomputed
    here: this answers only whether reporting one number for both classes would hide something.
    """
    floor = MIN_TURNS_GAIN if floor is None else floor
    per, signs = {}, []
    for name in (VERIFIED, UNVERIFIED):
        c, p = turns_per_goal(control_by_class.get(name)), \
            turns_per_goal(candidate_by_class.get(name))
        if c is None or p is None:
            per[name] = None
            continue
        gain = round(c - p, 3)
        per[name] = gain
        if abs(gain) >= floor:
            signs.append(1 if gain > 0 else -1)
    if len(set(signs)) > 1:
        return {"disagree": True, "per_class": per,
                "why": "the classes moved in opposite directions past the %.2f floor (%s). "
                       "One number for both would report the cancellation as no difference."
                       % (floor, ", ".join("%s %+.2f" % (k, v) for k, v in per.items()
                                           if v is not None))}
    return {"disagree": False, "per_class": per, "why": ""}


# ------------------------------------------------------------------------------------------
# Headroom
# ------------------------------------------------------------------------------------------

#: What the observables can even express, measured over every goal this campaign has run.
#:
#: THE WORKLOAD IS SATURATED AND NO CANDIDATE CAN BE SHOWN TO HELP ON IT.
#:
#: Across 111 recorded goals, 96.4% finished in ONE turn and 98.2% reached DONE. Turns cannot
#: go below one and completion cannot go above all, so on this goal set the only direction any
#: instrument here can detect is DOWNWARD. A comparison can establish that a candidate did no
#: harm; it cannot establish that it helped, however long it runs and however well calibrated
#: the floor is.
#:
#: That is not a defect in the instruments. It is a property of the goals, and it explains a
#: result that looked like three separate disappointments: transport measured 245 MB against a
#: 300 MB floor, planner measured exactly 0.00, and a memory comparison would join them. Two of
#: those were about the effect being small. This one is about the ruler having no room left.
#:
#: WHAT WOULD CHANGE IT. Goals that routinely take three or four turns, where a harness that
#: primes better or plans better has something to save. Choosing them AFTER seeing a result
#: would be picking the workload to fit the answer, so the honest order is: pick the goals for
#: headroom first, re-measure the null spread on them, and only then run a treatment.
HEADROOM_OBSERVED = {"goals": 111, "one_turn_fraction": 0.964, "done_fraction": 0.982}


def headroom(path, goals=None) -> dict:
    """How much room the observables have left, from the log. Never raises.

    Reported so a comparison can say, before it spends twenty minutes, that the only finding
    available to it is a negative one.
    """
    import json as _json
    one_turn = total = done = 0
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = _json.loads(line)
                except Exception:
                    continue
                if row.get("event") != "worker_done":
                    continue
                total += 1
                if int(row.get("turns", 0) or 0) <= 1:
                    one_turn += 1
                if str(row.get("outcome", "")).upper() == "DONE":
                    done += 1
    except Exception:
        pass
    if not total:
        return {"goals": 0, "one_turn_fraction": None, "done_fraction": None,
                "improvement_detectable": None, "why": "no rows to judge headroom from"}
    otf, df = one_turn / total, done / total
    tight = otf >= 0.9 and df >= 0.9
    return {"goals": total, "one_turn_fraction": round(otf, 3), "done_fraction": round(df, 3),
            "improvement_detectable": not tight,
            "why": ("%.0f%% of goals already finish in one turn and %.0f%% already reach DONE. "
                    "Turns cannot go below one and completion cannot exceed all, so a "
                    "comparison on this set can only detect harm." % (otf * 100, df * 100))
            if tight else ""}
