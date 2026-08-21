"""The first real evaluator: route policies, measured against the control that already exists.

WHY THIS FAMILY AND NOT THE OTHERS

The evolution loop has never completed a measured experiment. Wiring the CompanionBench
evaluator to it produced, in one run, four refusals -- and every one was correct: a memory
experiment needs a seeded store or the component is never exercised; `max_refute_passes` is
inert while the refuter is off; the fleet target does not read `components.quality_cards`, so
both arms would run the same program. The machinery was not broken. Every coordinate it was
offered genuinely could not be measured.

Route policy is the one hypothesis family with a control group that already exists and costs
nothing to construct. The socket route and the tab route run the SAME goals through the SAME
fleet and differ only in transport, which is what an arm is supposed to be. There is no seed
to build, no component to reach, and nothing inert.

WHAT IS MEASURED, FIXED BEFORE THE RUN

    peak commit of processes the arm CREATED    the reason the route exists
    wall clock                        the other reason
    goals reaching DONE               the thing that must not get worse
    fallbacks                         how often the route gave up and opened a tab anyway

THE THIRD VERDICT IS NOT A COURTESY

`DONE_candidate < DONE_control` is a REJECT. Better memory with equal completion is a KEEP.
Everything else is INCONCLUSIVE, and that is a different claim from either -- "we ran it and
learned nothing" is not "it was worse". The hypothesis ledger already separates INFRA_ABORT
from a verdict for the same reason; this applies the same discipline to the result side, where
collapsing it would teach the optimiser that a change was harmful when the run merely could
not tell.

TWO PRECONDITIONS THAT REFUSE RATHER THAN MEASURE

  * free memory below the floor. The quantity under test IS memory, so an arm that runs while
    the machine is swapping measures the swap. One arm was lost to this on 2026-08-21.
  * no usable socket token. Without one the socket arm silently falls back to tabs and the two
    arms become the same program -- the exact defect every refusal above was protecting
    against, arriving through the door marked "the experiment ran fine".
"""
from __future__ import annotations

import time

#: Free physical memory an arm needs. THE OPERATOR'S NUMBER, not one this file invented.
#:
#: The first value here was 2000, borrowed from the reviewer-page constant on the reasoning
#: that "below it the machine is not running the thing you think it is". That was never
#: calibrated for this measurement and it made every run on the operator's box abort. The
#: fleet's own admission floor is the operator's setting (the cockpit ships 2048 and a
#: recorded live run used 1024); 512 is the value they set for this machine.
#:
#: THOSE TWO FLOORS ARE NOT THE SAME QUANTITY. The fleet's `ram_floor_mb` asks "may I open
#: another tab without taking RAM the operator is using". This one asks "are the numbers I am
#: writing down about memory, or about the page file". They share a unit and nothing else, so
#: this one does NOT track the cockpit setting -- it is set deliberately, here, and the reason
#: is written next to it.
#:
#: What survives the lower floor is the SECOND-ORDER problem, which the floor never addressed:
#: under memory pressure Windows trims working sets, so RSS under-reports, and it under-reports
#: more for whichever arm ran under more pressure. Arms run in sequence, so the second one
#: inherits the first one's residue. `start_mb` is recorded per arm and the arm order is
#: recorded with the result, because that bias is not something a floor can remove.
MIN_FREE_MB = 512.0

#: How much less peak memory the candidate must use before "better" is claimed. Set from the
#: measured gap between the routes (+205 MB against +1653 MB) -- generous enough that noise
#: cannot clear it, far below what the route actually delivers when it works.
MIN_MEMORY_GAIN_MB = 300.0


class RouteRefusal(RuntimeError):
    """The comparison cannot be run honestly. Not a result about the routes."""


def preflight(*, free_mb, token_ok) -> list:
    """Reasons this comparison must not run. Empty means it may."""
    reasons = []
    if free_mb is not None and free_mb < MIN_FREE_MB:
        reasons.append(
            "%.0f MB free, floor %.0f (the operator's setting for this machine). The quantity "
            "under test is memory, so an arm that runs while the machine swaps measures the "
            "swap." % (free_mb, MIN_FREE_MB))
    if not token_ok:
        reasons.append(
            "no usable socket token. The socket arm would fall back to tabs and the two arms "
            "would be the same program, which is the one thing an A/B may never be.")
    return reasons


def decide(control, candidate, *, min_gain_mb=MIN_MEMORY_GAIN_MB) -> dict:
    """The verdict, from two arms' measurements. Pure -- no clock, no machine, no fleet.

    Kept separate from the running so it can be tested against numbers rather than against a
    live fleet, which is the only way the decision rule itself gets checked.
    """
    done_c = int(control.get("done", 0))
    done_p = int(candidate.get("done", 0))
    gain = float(control.get("peak_mb", 0.0)) - float(candidate.get("peak_mb", 0.0))

    if done_p < done_c:
        return {"verdict": "reject", "memory_gain_mb": round(gain, 1),
                "why": "completion fell: %d of %d against the control's %d. A route is a "
                       "speed-up and never a capability, so any loss here settles it "
                       "whatever the memory says." % (done_p, candidate.get("goals", 0), done_c)}
    if gain >= min_gain_mb:
        return {"verdict": "keep", "memory_gain_mb": round(gain, 1),
                "why": "completion held at %d and peak memory fell by %.0f MB (floor %.0f)."
                       % (done_p, gain, min_gain_mb)}
    return {"verdict": "inconclusive", "memory_gain_mb": round(gain, 1),
            "why": "completion held at %d but peak memory moved only %.0f MB, under the "
                   "%.0f MB this run can distinguish from noise. That is not a finding that "
                   "the route is worse." % (done_p, gain, min_gain_mb)}


def measure_arm(run_goals, *, goals, socket_on, peak_sampler, now=time.time) -> dict:
    """Run one arm and return what was fixed in advance, nothing else.

    `run_goals(goals, socket_on)` is supplied by the caller so this module never imports the
    fleet: the decision rule and the measurement shape stay testable without a browser.

    `peak_sampler()` returns a memory figure in MB and the arm's peak is reported as a RISE
    over its own start. WHAT THE CALLER PUTS BEHIND THAT SAMPLER DECIDES WHETHER THE NUMBER
    MEANS ANYTHING. The first caller sampled total Edge RSS, and two campaigns run with the
    arms swapped returned opposite signs: on a browser shared with other sessions, total RSS
    is not attributable to an arm, Windows trims working sets so it tracks system pressure
    rather than demand, and the second arm inherits a high-water mark. The sampler now returns
    the commit charge of processes that did not exist when the arm began, which is a quantity
    the arm actually caused. A rise over a start of zero, but the shape is unchanged and this
    module still does not need to know which it was given.
    """
    start_mb = float(peak_sampler() or 0.0)
    peak = start_mb
    t0 = now()

    def sample():
        nonlocal peak
        value = float(peak_sampler() or 0.0)
        if value > peak:
            peak = value

    out = run_goals(goals, socket_on, sample) or {}
    sample()
    return {
        "socket": bool(socket_on),
        "goals": len(goals),
        "done": int(out.get("done", 0)),
        "fallbacks": int(out.get("fallbacks", 0)),
        "wall_s": round(now() - t0, 1),
        "peak_mb": round(peak - start_mb, 1),
        "start_mb": round(start_mb, 1),
    }
