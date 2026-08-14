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


def decide(*, gate=None, sentinel=None, security=None, regression=None,
           infra=None, frozen_ok=True, auto_apply=False) -> dict:
    """Combine the gates into one state plus the reason it was reached.

    Every argument is optional and None means "not evaluated", which is deliberately NOT
    the same as "passed". Under auto_apply an unevaluated REQUIRED gate escalates to
    NEEDS_HUMAN_REVIEW rather than being skipped -- the same fail-closed rule Phase 0
    applied to the sentinel, for the same reason: absence of evidence reads as success
    unless something makes it not.
    """
    reasons = []

    # 0. the judge itself
    if not frozen_ok:
        return _out(INFRA_ABORT, "frozen set changed: the judge was not intact, so no "
                                 "result from this run can be trusted", reasons)

    # 1. did the run happen at all
    if infra and infra.get("aborted"):
        return _out(INFRA_ABORT, infra.get("reason") or "infrastructure abort", reasons)

    # 2. security, before any question of usefulness
    if security is not None:
        if security.get("regressed"):
            return _out(SECURITY_REJECT,
                        security.get("reason") or "a security episode regressed", reasons)
        reasons.append("security: no regression")
    elif auto_apply:
        return _out(NEEDS_HUMAN_REVIEW,
                    "security was not evaluated; auto-apply requires it", reasons)

    # 3. previously-passing work must still pass
    if regression is not None:
        if regression.get("regressed"):
            return _out(REGRESSION_REJECT,
                        regression.get("reason") or "a previously-passing episode broke",
                        reasons)
        reasons.append("regression: none")
    elif auto_apply:
        return _out(NEEDS_HUMAN_REVIEW,
                    "the regression pool was not run; auto-apply requires it", reasons)

    # 4. the cross-dataset canary
    if sentinel is not None:
        if sentinel.get("unevaluable"):
            if auto_apply:
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
