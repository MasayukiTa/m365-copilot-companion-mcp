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
#: None until a null run measures how far two IDENTICAL arms land apart on this number. Any
#: value put here before that is invented, and this repository has already run a full day on an
#: invented threshold: 300 MB came from an unrelated constant, then from measurements that were
#: reading arm order, and only meant something once two null runs put identical arms 130-180 MB
#: apart. `decide` refuses while this is None rather than guessing.
MIN_TURNS_GAIN = None

#: Free physical memory an arm needs. Same quantity and same operator setting as the route
#: evaluator's -- a fleet that swaps does not run the thing you think it does, whatever is
#: being measured.
MIN_FREE_MB = 512.0


class NotCalibrated(RuntimeError):
    """A verdict was asked for before the instrument had a measured noise floor."""


def preflight(*, free_mb, calibrated=None) -> list:
    """Reasons this comparison must not run. Empty means it may."""
    reasons = []
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
    """The arm's turns per goal, or None when it completed nothing to divide by."""
    goals = int((arm or {}).get("goals", 0) or 0)
    if goals <= 0:
        return None
    return float((arm or {}).get("turns", 0) or 0) / goals


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
    return {"verdict": "inconclusive", "turns_gain": round(gain, 3),
            "why": "completion held at %d but turns per goal moved only %.2f, under the %.2f "
                   "this instrument can distinguish from noise. That is not a finding that "
                   "either harness is worse." % (done_p, gain, floor)}
