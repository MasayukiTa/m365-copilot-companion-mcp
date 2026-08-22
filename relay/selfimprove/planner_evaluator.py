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

THE THRESHOLD IS DELIBERATELY UNSET

`MIN_TURNS_GAIN` is None and `decide()` refuses while it is. The memory floor spent a day as a
number borrowed from an unrelated constant, survived a threshold derivation that turned out to
be measuring arm order, and only became meaningful when a null run gave it a spread to sit
above. Starting this one uncalibrated is not an oversight to fix later -- it is the state that
tells the truth until a null run has been done, and it fails loudly rather than producing a
verdict nobody should trust.

WHY TURNS

A planner version that plans first spends a turn planning. That is a mechanism, not a
correlation: `planner/v2`'s opening turn is measurably longer than `planner/v1`'s on every goal
in the current set, and the extra turn either pays for itself in fewer later turns or it does
not. `worker_done.turns` already carries the count per goal, so the observable exists and does
not need building.
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
#: The mechanism it is meant to see is far above this. `planner/v2` spends a turn planning, so
#: it should move turns per goal by about 1.0 -- larger than the whole observed noise range,
#: and a much easier ratio than the memory instrument ever had (245 MB of effect against a
#: 130-180 MB floor).
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


def preflight(*, free_mb, calibrated=None, observable_recorded=True) -> list:
    """Reasons this comparison must not run. Empty means it may.

    `observable_recorded` is the one this instrument needs that the memory one does not.
    `worker_done` rows -- where turns live -- are written by `socket_route.record`, which is a
    NO-OP while the route is disabled. A run with both arms on tabs therefore produces a
    complete, ordinary-looking result with zero rows to count, and the only way to find out is
    to wait for it. Learned by waiting thirty minutes for exactly that, after writing the
    warning down in the coordinate audit and then launching the run anyway.
    """
    reasons = []
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


def decide(control, candidate, *, min_gain=None) -> dict:
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
    return {"verdict": "inconclusive", "turns_gain": round(gain, 3),
            "why": "completion held at %d and turns per goal moved %.2f, inside the %.2f this "
                   "instrument can distinguish from noise. That is not a finding that either "
                   "harness is worse." % (done_p, gain, floor)}
