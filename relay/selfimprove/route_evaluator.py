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
#: What it rests on is a null experiment: the same comparison run with BOTH arms set to the
#: control, so the two arms are the same program and every difference reported is the
#: instrument's own spread.
#:
#: THE DERIVATION RECORDED HERE FIRST WAS ITSELF FROM THE WRONG INSTRUMENT, and a reader acted
#: on it. It cited two nulls landing 130-180 MB apart; both were run BEFORE the arms stopped
#: sharing a memory store, so they measured a spread that channel was contributing to. On
#: 2026-08-23 that stale figure was read as evidence the floor was far too high, on the
#: strength of four current-instrument runs that happened to look tight. It was not.
#:
#: TWELVE RUNS, CURRENT INSTRUMENT, INTERLEAVED SO A DRIFT OVERNIGHT COULD NOT LINE UP WITH A
#: CONDITION, on a goal set with the 8.3 short path removed (which had cost one arm 33 minutes
#: on a single turn):
#:
#:     null, socket vs socket    -89.1  -40.9  -30.7  +61.5
#:     null, tabs vs tabs        -46.8  +22.2  +114.3  +184.2
#:     treatment, tabs vs socket -126.8  +34.4  +180.4  +252.4
#:
#: Identical arms land up to 273 MB apart and a single null pair reached +184 MB, so 300 is
#: about right and is NOT too high. The treatment mean sits 63.3 MB above the null mean, an
#: exact permutation test gives p=0.21 against a floor of 0.002 for these counts, and the
#: pooled SD is 119 MB: detecting an effect that size at 80% power needs roughly 55 runs per
#: group, about 111 runs and 17 hours. The effect, if any, is SMALLER than the noise; this
#: design lacks the power to detect it and no threshold repairs that.
#:
#: TWO EARLIER READINGS OF THIS SAME QUESTION DID NOT SURVIVE. A null taken only under the
#: socket condition gave a complete separation at p=0.0143; adding tabs-vs-tabs nulls dropped
#: it to p=0.0238; removing the short path from the goal set took it to p=0.21. Each step made
#: the ruler more honest and the finding smaller. `run_archive` holds the runs and the rule for
#: which of them may be put in one column, so this does not have to be reconstructed by hand
#: again -- doing it by hand is what produced both wrong readings.
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


#: Free disk an arm needs before it may start, in GB.
#:
#: NOT A NUMBER THIS FILE CHOSE. `relay_fleet.DEFAULT_DISK_FLOOR_GB` is the floor the fleet's
#: admission gate already enforces; this reads the same value so the two cannot disagree. A
#: preflight with its own floor would pass a run the fleet then refuses to admit, which is
#: exactly the failure this exists to prevent.
#:
#: WHY IT IS A PRECONDITION AND NOT SOMETHING TO DISCOVER LATER. Below the fleet's floor the
#: admission gate declines every worker and simply keeps sweeping: no log line, no error, no
#: terminal state. Two calibration runs sat at `status=pending, turn=0` for twenty-five minutes
#: each and looked entirely healthy doing it -- the process was alive, the browser was fine,
#: and the only symptom was silence. Measured by reproducing it with a stack dump: free disk
#: was 5.2 GB against a 6 GB floor.
def _fleet_disk_floor_gb():
    try:
        from relay.relay_fleet import DEFAULT_DISK_FLOOR_GB
        return float(DEFAULT_DISK_FLOOR_GB)
    except Exception:
        return 6.0


class RouteRefusal(RuntimeError):
    """The comparison cannot be run honestly. Not a result about the routes."""


def preflight(*, free_mb, token_ok, free_disk_gb=None) -> list:
    """Reasons this comparison must not run. Empty means it may."""
    reasons = []
    if free_disk_gb is None:
        # THE FLEET'S OWN READER, not a second one. `relay_fleet.free_disk_gb` is what the
        # admission gate measures with; reading the disk a different way here would let this
        # preflight and that gate disagree about the same drive.
        try:
            from relay.relay_fleet import free_disk_gb as _fleet_free
            free_disk_gb = float(_fleet_free())
        except Exception:
            free_disk_gb = None
    floor_gb = _fleet_disk_floor_gb()
    if free_disk_gb is not None and free_disk_gb < floor_gb:
        reasons.append(
            "%.2f GB free on C:, and the fleet's admission gate needs %.1f GB. Below that it "
            "declines every worker and keeps sweeping in silence -- no log line, no error, no "
            "terminal state -- so the run looks healthy for as long as you are willing to wait. "
            "Free disk; do not lower the floor, which converts a refusal into a crash."
            % (free_disk_gb, floor_gb))
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
    # A LARGE NEGATIVE GAIN IS A FINDING. It was folded into the null case, and the sentence
    # then claimed the number was "under the floor" -- of -666 MB against a floor of 300, which
    # is more than twice it in the other direction. That run was read as inconclusive and moved
    # past. Detecting that the candidate costs memory is what this instrument is for.
    if gain <= -min_gain_mb:
        return {"verdict": "reject", "memory_gain_mb": round(gain, 1),
                "task_fallback_rate": route.get("task_rate"),
                "why": "completion held at %d and peak memory ROSE by %.0f MB, past the %.0f MB "
                       "this run can distinguish from noise. That is a measured cost, not an "
                       "absence of evidence.%s" % (done_p, -gain, min_gain_mb, note)}
    return {"verdict": "inconclusive", "memory_gain_mb": round(gain, 1),
            "task_fallback_rate": route.get("task_rate"),
            "why": "completion held at %d and peak memory moved %.0f MB, inside the "
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
