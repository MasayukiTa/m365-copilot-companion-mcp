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

#: How much less memory the candidate must use before "better" is claimed.
#:
#: THE ORIGINAL DERIVATION WAS WORTHLESS AND THE NUMBER SURVIVED ANYWAY.
#:
#: It was set from a measured gap between the routes (+205 MB against +1653 MB) taken with the
#: total-RSS sampler, which later turned out to be measuring which arm ran first. A threshold
#: calibrated against an instrument that was reading the wrong quantity is not calibrated.
#:
#: What it now rests on is a null experiment: the same comparison run with BOTH arms set to the
#: control, so the two arms are the same program and every difference reported is the
#: instrument's own spread. Two null runs, in both arm orders:
#:
#:     null, control first      291.7 MB vs 471.5 MB     reported "gain" -179.8 MB
#:     null, candidate first    237.3 MB vs 107.0 MB     reported "gain" -130.3 MB
#:
#: So identical arms land 130-180 MB apart, and 300 sits above that. The number is unchanged --
#: which matters, because the two treatment runs measured +94.9 MB and -11.5 MB, and a
#: threshold moved after seeing those would have been the ruler being cut to fit the object.
#: The honest reading of those runs is that the effect, if any, is SMALLER than the noise: this
#: design lacks the power to detect it, and no threshold can repair that.
MIN_MEMORY_GAIN_MB = 300.0



#: WHAT THIS INSTRUMENT CAN SEE, DECLARED WHERE THE INSTRUMENT IS.
#:
#: The dependent variable is the commit charge a run creates in Edge. That responds to how many
#: renderers a harness makes the fleet open, and to nothing else -- a harness differing only in
#: `memory_max_items` or `max_retries` has no mechanism to move it, so a comparison of two such
#: harnesses returns INCONCLUSIVE for a structural reason and twenty minutes buy nothing.
#:
#: DECLARED HERE RATHER THAN IN THE CALLER. The scope is a property of the measurement, and the
#: first version had the caller hold a list of what this file could see -- so the day a second
#: evaluator arrives, the caller's list describes whichever one was written first and nothing
#: says so. A measurement that cannot state its own range will have a range attributed to it.
#:
#: This is a claim about MECHANISM, not a measured sensitivity. `transport` is here because a
#: socket avoids opening a renderer; if a component is added whose effect on renderer count is
#: argued rather than demonstrated, the honest move is to leave it out until a null run says
#: otherwise.
MEASURES = ("transport",)

#: One line, shown to an operator deciding whether a comparison is worth twenty minutes.
MEASURES_NOTE = ("the commit charge a run creates in Edge, which moves with how many "
                 "renderers the harness makes the fleet open")


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



#: Task-caused fallbacks per goal that an arm may show before the comparison is worth doubting.
#:
#: NOT CALIBRATED, AND SAYING SO IS THE POINT. Across 22 recorded arms and 88 goals the observed
#: task-caused fallback count is ZERO, so there is no measured baseline to set a rate against --
#: and at four goals per arm the finest rate a run can even express is 25%. A threshold invented
#: on top of that would be a number wearing a calibration's clothes, which is exactly what the
#: 300 MB memory floor was before a null run gave it one.
#:
#: So this is a REPORTING threshold, not a gate: exceeding it annotates the result and does not
#: decide it. It becomes a gate the day a null run produces a non-zero baseline to compare
#: against, and the comment above is what tells the next person that day has not come.
TASK_FALLBACK_NOTE_RATE = 0.25


def fallback_verdict(control, candidate) -> dict:
    """Did either arm stop being the arm it claims to be? Pure; no clock, no fleet.

    THE HAZARD IS NOT THE COST OF A FALLBACK. IT IS THE ROUTE CLOSING.

    A fallback costs one turn and one tab, which is small and priced in the memory figure. What
    is not priced is the circuit breaker: after three consecutive failures, or ten in a run, the
    route closes ONE-WAY and every remaining goal opens a tab. From that moment the candidate
    arm IS the control arm -- the same-program defect this repository has now found seven ways
    into, arriving an eighth way, in the middle of a run, with both arms reporting ordinary
    numbers afterwards.

    That is not a rate question and needs no calibration: `closed_reason` says it exactly.

    Returns {"aborted": bool, "why": str, "task_rate": float|None}. An arm that closed makes the
    comparison INFRA rather than a verdict, because "we learned nothing, the instrument changed
    underneath" is a different claim from "the two are indistinguishable" -- the distinction the
    hypothesis ledger has kept since the beginning and that the result side kept losing.
    """
    for name, arm in (("control", control), ("candidate", candidate)):
        closed = str((arm or {}).get("route_closed_reason") or "")
        if closed:
            return {"aborted": True, "task_rate": None,
                    "why": "the route closed during the %s arm (%s). Every goal after that "
                           "point opened a tab, so from there the two arms were the same "
                           "program -- the comparison stopped measuring transport partway "
                           "through and the numbers after it are of something else"
                           % (name, closed[:120])}

    rates = []
    for arm in (control, candidate):
        goals = int((arm or {}).get("goals", 0) or 0)
        task = int((arm or {}).get("task_fallbacks", 0) or 0)
        rates.append((task / goals) if goals else 0.0)
    worst = max(rates) if rates else 0.0
    if worst > TASK_FALLBACK_NOTE_RATE:
        return {"aborted": False, "task_rate": round(worst, 3),
                "why": "%.0f%% of goals fell back for task reasons, above the %.0f%% this run "
                       "annotates at. Recorded, not gated: there is no measured baseline for "
                       "this rate yet." % (worst * 100, TASK_FALLBACK_NOTE_RATE * 100)}
    return {"aborted": False, "task_rate": round(worst, 3), "why": ""}


def decide(control, candidate, *, min_gain_mb=MIN_MEMORY_GAIN_MB) -> dict:
    """The verdict, from two arms' measurements. Pure -- no clock, no machine, no fleet.

    Kept separate from the running so it can be tested against numbers rather than against a
    live fleet, which is the only way the decision rule itself gets checked.
    """
    # FIRST, BEFORE ANY NUMBER IS READ. An arm whose route closed is not the arm the row says
    # it is, and comparing its memory to the other one's measures something nobody asked about.
    route = fallback_verdict(control, candidate)
    if route.get("aborted"):
        return {"verdict": "inconclusive", "aborted": True, "memory_gain_mb": None,
                "why": route["why"]}

    done_c = int(control.get("done", 0))
    done_p = int(candidate.get("done", 0))
    gain = float(control.get("peak_mb", 0.0)) - float(candidate.get("peak_mb", 0.0))

    note = (" " + route["why"]) if route.get("why") else ""
    if done_p < done_c:
        return {"verdict": "reject", "memory_gain_mb": round(gain, 1),
                "task_fallback_rate": route.get("task_rate"),
                "why": "completion fell: %d of %d against the control's %d. A route is a "
                       "speed-up and never a capability, so any loss here settles it "
                       "whatever the memory says.%s" % (done_p, candidate.get("goals", 0), done_c, note)}
    if gain >= min_gain_mb:
        return {"verdict": "keep", "memory_gain_mb": round(gain, 1),
                "task_fallback_rate": route.get("task_rate"),
                "why": "completion held at %d and peak memory fell by %.0f MB (floor %.0f)."
                       "%s" % (done_p, gain, min_gain_mb, note)}
    return {"verdict": "inconclusive", "memory_gain_mb": round(gain, 1),
            "task_fallback_rate": route.get("task_rate"),
            "why": "completion held at %d but peak memory moved only %.0f MB, under the "
                   "%.0f MB this run can distinguish from noise. That is not a finding that "
                   "the route is worse.%s" % (done_p, gain, min_gain_mb, note)}


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
