"""The verdict, as one function with an ordered set of gates.

Phase 4 asks for a closed loop whose outcome is not pass/fail. That distinction is the
whole substance: collapsing eight possible outcomes into two throws away the information a
human needs to decide what to do next, and worse, it pushes ambiguous results into whichever
bucket the collapse favours.

    KEEP                everything passed
    REJECT              the measurement says the change is not an improvement
    INCONCLUSIVE        it ran, the difference is inside the noise -- the common case
    INFRA_ABORT         we learned nothing because the harness broke
    SECURITY_REJECT     a security episode regressed
    SENTINEL_REJECT     the cross-dataset canary regressed
    REGRESSION_REJECT   a previously-passing episode broke
    NEEDS_HUMAN_REVIEW  the gates disagree, or something required was unevaluable

ORDER MATTERS AND IS NOT ARBITRARY

Infra first: if the run did not happen, nothing downstream means anything, and grading a
broken run produces a number that looks real. Security next, before any question of
usefulness -- a change that improves the pass rate by breaking an injection defence is not
a trade to be weighed, it is a rejection. Then regression, then the sentinel, then finally
the statistical gate, which is the only one that can say KEEP.

INCONCLUSIVE IS NOT A FAILURE

Recorded as REJECT, a null result teaches the optimiser that the change was harmful and it
will avoid that direction. Recorded as KEEP, noise accumulates as progress. It gets its own
state so the common outcome can be told the truth.
"""
from __future__ import annotations

KEEP = "KEEP"
REJECT = "REJECT"
INCONCLUSIVE = "INCONCLUSIVE"
INFRA_ABORT = "INFRA_ABORT"
SECURITY_REJECT = "SECURITY_REJECT"
SENTINEL_REJECT = "SENTINEL_REJECT"
REGRESSION_REJECT = "REGRESSION_REJECT"
NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"

STATES = (KEEP, REJECT, INCONCLUSIVE, INFRA_ABORT, SECURITY_REJECT,
          SENTINEL_REJECT, REGRESSION_REJECT, NEEDS_HUMAN_REVIEW)

#: States that may activate a candidate. Everything else must not, including the ones that
#: merely failed to prove harm.
ACTIVATING = frozenset({KEEP})


def _evaluated(result) -> bool:
    """True only when a gate actually reports something.

    `{}` is not an evaluation. It was accepted as one, so any caller could hand in an empty
    dict -- including by accident, since that is what "no findings" looks like -- and buy a
    pass for a gate that never ran. A gate must say what it checked.
    """
    return isinstance(result, dict) and bool(result)


def decide(*, gate=None, sentinel=None, security=None, regression=None,
           infra=None, frozen_ok=True, auto_apply=False, will_activate=False) -> dict:
    """Combine the gates into one state plus the reason it was reached.

    Every argument is optional and None means "not evaluated", which is deliberately NOT
    the same as "passed", and neither is `{}` -- an empty dict is what "no findings" looks
    like and it used to buy a pass for a gate that never ran.

    `will_activate` is what actually gates the requirements, NOT auto_apply. Those were
    separate flags, so EvolutionController(activate=True, auto_apply=False) skipped the
    security requirement entirely and then wrote the manifest. The dangerous act is
    ACTIVATION; whether a human pressed the button is irrelevant to whether the candidate
    was checked. Either flag now demands the same evidence.
    """
    strict = bool(auto_apply or will_activate)
    reasons = []

    # 0. the judge itself
    if not frozen_ok:
        return _out(INFRA_ABORT, "frozen set changed: the judge was not intact, so no "
                                 "result from this run can be trusted", reasons)

    # 1. did the run happen at all
    if infra and infra.get("aborted"):
        return _out(INFRA_ABORT, infra.get("reason") or "infrastructure abort", reasons)

    # 2. security, before any question of usefulness
    if _evaluated(security):
        if security.get("regressed"):
            return _out(SECURITY_REJECT,
                        security.get("reason") or "a security episode regressed", reasons)
        # A regression check alone is not a security gate: if every security episode fails
        # on BOTH arms there is no regression to find, and a candidate that passes none of
        # them sails through. Require positive evidence that something actually held.
        #
        # "no security episode was RUN" and "none of them PASSED" are different facts and
        # must not collapse into one verdict. Zero comparable episodes is an unevaluated
        # gate -- which under strict escalates for review -- while zero passes out of some
        # is a rejection. Conflating them would reject every suite that has no security
        # episodes at all, and hide the case worth catching.
        comparable = security.get("comparable")
        passed = security.get("passed_count")
        if comparable is not None and not comparable:
            if strict:
                return _out(NEEDS_HUMAN_REVIEW,
                            "no security episode was comparable across the arms; there is "
                            "nothing to conclude from", reasons)
            reasons.append("security: nothing comparable to check")
        elif passed is not None and not passed:
            return _out(SECURITY_REJECT,
                        "no security episode passed on the candidate; a regression check "
                        "cannot see a floor that was already zero", reasons)
        else:
            reasons.append("security: no regression")
    elif strict:
        return _out(NEEDS_HUMAN_REVIEW,
                    "security was not evaluated; activation requires it", reasons)

    # 3. previously-passing work must still pass
    if _evaluated(regression):
        if regression.get("regressed"):
            return _out(REGRESSION_REJECT,
                        regression.get("reason") or "a previously-passing episode broke",
                        reasons)
        # Passed on the baseline, unrunnable on the candidate. Not a proven regression, but
        # crashing the episode you are about to break is how a regression check is defeated,
        # so uncertainty here must not read as a pass when something will be activated.
        if regression.get("unevaluable"):
            if strict:
                return _out(NEEDS_HUMAN_REVIEW,
                            regression.get("reason")
                            or "previously-passing episodes became unrunnable on the "
                               "candidate only; that is not evidence of no regression",
                            reasons)
            reasons.append("regression: %d previously-passing episode(s) unevaluable"
                           % len(regression["unevaluable"]))
        else:
            reasons.append("regression: none")
    elif strict:
        return _out(NEEDS_HUMAN_REVIEW,
                    "the regression pool was not run; activation requires it", reasons)

    # 4. the cross-dataset canary
    #
    # `{}` REACHED THE "CONFIGURED" BRANCH AND READ AS "no regression", so an empty dict --
    # which is what a caller produces by accident, and what a gate that never ran looks like
    # -- bought a KEEP under activation. The same mistake `_evaluated` was written to stop
    # for the other gates, left in the one gate whose whole job is to catch a result that
    # only looks good.
    if not _evaluated(sentinel):
        # NOT CONFIGURING THE TRIPWIRE DISABLED THE TRIPWIRE. A deleted sentinel file was
        # made to fail closed, but the default -- no sentinel path at all -- still sailed
        # straight through to the statistical gate and could activate. That makes the
        # strongest guard against a grader-specific gain optional by omission, which is the
        # easiest kind of guard to lose. Under strict it is now required like the others;
        # without strict it stays a note, because a run that only reports is allowed to be
        # partial.
        if strict:
            return _out(NEEDS_HUMAN_REVIEW,
                        "no sentinel was configured; activation requires the cross-dataset "
                        "canary, and omitting it is not the same as passing it", reasons)
        reasons.append("sentinel: not configured (gate-only, not activating)")
    else:
        if sentinel.get("unevaluable"):
            if strict:
                return _out(NEEDS_HUMAN_REVIEW,
                            "sentinel configured but unevaluable: uncertainty must not read "
                            "as success", reasons)
            reasons.append("sentinel: unevaluable (queued for review)")
        elif sentinel.get("regressed"):
            return _out(SENTINEL_REJECT,
                        sentinel.get("reason") or "sentinel regressed: the gain looks "
                                                  "grader- or dataset-specific", reasons)
        else:
            reasons.append("sentinel: no regression")

    # 5. only now, is it actually better
    if gate is None:
        return _out(NEEDS_HUMAN_REVIEW, "no statistical gate result", reasons)
    if gate.get("keep"):
        return _out(KEEP, gate.get("reason") or "gate: significant improvement", reasons)
    if gate.get("verdict") in ("suggestive", "inconclusive", "underpowered"):
        # Positive direction, not enough evidence. The correct action is a larger N, and
        # calling it REJECT would teach the optimiser to avoid a direction that may be right.
        return _out(INCONCLUSIVE,
                    gate.get("reason") or "difference is inside the noise; enlarge N",
                    reasons)
    return _out(REJECT, gate.get("reason") or "gate: not an improvement", reasons)


def _out(state, reason, reasons):
    return {
        "state": state,
        "reason": reason,
        "passed_gates": list(reasons),
        "may_activate": state in ACTIVATING,
    }


def summarise(decisions) -> dict:
    """Counts by state, for a campaign report.

    Kept separate from any notion of a score. The useful reading of a campaign is its
    SHAPE -- lots of INCONCLUSIVE means the slices are too small, lots of INFRA_ABORT means
    the harness is unwell, and neither is visible in a keep-rate.
    """
    counts = {s: 0 for s in STATES}
    for d in decisions or []:
        state = d.get("state") if isinstance(d, dict) else str(d)
        counts[state] = counts.get(state, 0) + 1
    total = sum(counts.values())
    return {
        "counts": counts,
        "total": total,
        "activated": counts[KEEP],
        # Not "success rate". A campaign of 40 experiments that activates 2 may be healthy;
        # one that activates 30 is almost certainly measuring itself.
        "inconclusive_share": (counts[INCONCLUSIVE] / total) if total else None,
        "infra_share": (counts[INFRA_ABORT] / total) if total else None,
    }
