"""Every way a worker can finish, as a closed set, with a total mapping to what a reader sees.

WHY THIS EXISTS. The mapping from outcome to reported status was a chain of `if o == "..."`
ending in `return "error"`, and that final line misreported healthy work twice:

  * INFRA_STUCK and REFUSED were listed in RETRYABLE_OUTCOMES a thousand lines above -- the
    same file already knew they meant "no answer yet", not "the task was wrong" -- and still
    fell through to "error". INFRA_STUCK exists precisely so a connection that never
    established is not scored as a failed task.
  * FANOUT, added when a goal became able to split, was not in the chain either. A run whose
    nine subtasks all completed, merged, and wrote its answer to disk reported 0 done of 1.

Both were the same defect, and neither was a typo: a catch-all cannot distinguish "a value
that means failure" from "a value nobody has added yet", so every new outcome is silently
born as an error. The chain gave no signal at the point where the outcome was invented.

So the set is closed and the mapping is total. `status_of` RAISES on anything unlisted, and
an exhaustiveness test walks the AST of the relay package for every literal assigned to
`.outcome`, so the raise is unreachable from production: a new outcome fails the test on the
commit that introduces it, which is the moment somebody knows what it should mean.

THE PARTITION IS CHECKED, NOT ASSUMED. Retryable and non-retryable were two hand-kept
frozensets naming six outcomes between them, out of eleven. The remaining five were
non-retryable only by omission -- indistinguishable, again, from not having been considered.
"""
from __future__ import annotations


class UnknownOutcome(ValueError):
    """An outcome no one has classified. Never caught to produce a default."""


#: outcome -> the status a reader sees. Total over OUTCOMES by construction: the module
#: refuses to import if the two disagree (see the assertions at the foot of this file).
STATUS_OF = {
    "DONE": "done",
    #: A goal that SPLIT did its job and handed the work to the children it spawned. It is
    #: done AS A GOAL; the answer arrives from the merge that follows its family.
    "FANOUT": "done",
    "MAXTURNS": "maxturns",
    "CANCELLED": "cancelled",
    "CONTENT_REFUSED": "content_refused",
    "STUCK": "stuck",
    #: The connection or agent never established. Reporting this as an error is the misreport
    #: the outcome was created to prevent.
    "INFRA_STUCK": "stuck",
    #: The agent answered and Copilot declined this prompt. Measured transient: 25% of goals
    #: across 28 in six runs, moving to a different goal each run.
    "REFUSED": "stuck",
    "VERIFY_FAILED": "stuck",
    #: The worker claimed DONE and the RECORD of what it did contradicts the claim -- it is the
    #: fleet-side counterpart of the benchmark's evidence check, assigned by _settle_done.
    #:
    #: MISSING UNTIL 2026-09-04, and missing for a reason worth keeping: _settle_done
    #: consolidated four separate ("done", "DONE") assignments into one site, which is exactly
    #: the right move -- but it turned the literal into a RETURN value, and the exhaustiveness
    #: walker only read assignments. So the guard that exists to keep this set closed went
    #: blind to a whole outcome, and reported the most common one, DONE, as never produced.
    #: Widening the walker to follow the method it assigns from surfaced this one immediately.
    #:
    #: "done", not "stuck": the work FINISHED. What is in doubt is whether it did what it
    #: claimed, and reporting that as stuck would tell an operator to re-run something that
    #: already ran to completion.
    "EVIDENCE_CONTRADICTED": "done",
    "ERROR": "error",
}

#: The closed set. Nothing outside it may be assigned to a worker's outcome.
OUTCOMES = frozenset(STATUS_OF)

#: Declared but never produced, with the reason, so the exhaustiveness test can tell "not
#: emitted yet" from "the enum is stale".
#:
#: EMPTY, AND THAT IS THE POINT. It held UNRESOLVED_REFUSAL, which was the first thing
#: closing this set found: an outcome, a status, a pill and a terminal-state entry -- five
#: places -- for a value no branch anywhere assigned. Keeping it was justified at the time by
#: not wanting to change the UI's vocabulary; that was the wrong way round, because the
#: vocabulary described a state the system cannot reach. All five are gone.
NOT_PRODUCED = {}

#: Worth another attempt: the run did not get an answer, as opposed to the task being wrong.
RETRYABLE = frozenset({"STUCK", "INFRA_STUCK", "REFUSED"})

#: NOT retried, and the distinction is the point. MAXTURNS means the worker spent its whole
#: turn budget: running it again spends the same budget the same way. CANCELLED was a human
#: saying stop. CONTENT_REFUSED is a judgement about the request, and
#: repeating a request unchanged does not change a judgement of it. VERIFY_FAILED produced an
#: answer that failed its acceptance check -- a retry is the caller's decision, not the
#: loop's. ERROR is an exception whose cause is not known to be transient.
NON_RETRYABLE = frozenset(OUTCOMES - RETRYABLE)

#: Finished as a goal, so a context-loss recovery must not resurrect it. NOT the complement of
#: RETRYABLE: INFRA_STUCK is deliberately absent, because it says OUR path looked unhealthy
#: rather than that the goal is broken, so a fresh browser context deserves another shot at it.
#: ERROR and VERIFY_FAILED are likewise re-runnable after a recovery.
FINISHED = frozenset({
    "DONE", "FANOUT", "MAXTURNS", "CANCELLED", "CONTENT_REFUSED",
    "STUCK",
    # The worker ran to the end; only the truth of its claim is in question.
    "EVIDENCE_CONTRADICTED",
})


def status_of(outcome) -> str:
    """The reported status for `outcome`. Raises rather than guessing.

    The raise is what makes the set closed. A default branch here would restore exactly the
    behaviour that reported a healthy fan-out as a failure -- and would do it silently, which
    is why neither of the two occurrences was noticed until somebody read a total.
    """
    try:
        return STATUS_OF[outcome]
    except (KeyError, TypeError):
        raise UnknownOutcome(
            "outcome %r is not in the closed set; add it to relay/outcomes.py with the "
            "status it should report, rather than letting it default" % (outcome,))


def is_retryable(outcome) -> bool:
    """Whether the loop should re-queue this. Unlisted outcomes raise, they do not fall through
    to 'no' -- 'not retryable' and 'not considered' were the same answer before, and the one
    outcome that actually occurs here (STUCK) was the one originally left out of the list."""
    if outcome not in OUTCOMES:
        raise UnknownOutcome("outcome %r is not in the closed set" % (outcome,))
    return outcome in RETRYABLE


#: Whether an outcome counts toward a measured pass rate, and on which side, WHEN NOTHING IS
#: KNOWN ABOUT WHAT THE WORKER DID. Total over OUTCOMES; `scoring_of` refines it with evidence.
#:
#: WHY A THIRD VALUE. Scoring code kept asking one question -- "is this DONE?" -- and every
#: outcome that was neither a pass nor a failure of the system under test got silently filed
#: as a failure. A run a human stopped is not a failure of the agent; it was not allowed to
#: finish, and counting it measures the operator.
#:
#: WHY THAT IS NOT THE WHOLE ANSWER. Excluding every stopped run is directly gameable, and not
#: only in theory: people stop the runs that look bad. Whatever is excluded leaves the
#: denominator, so a habit of stopping doomed runs raises the reported rate without the agent
#: solving anything more.
#:
#: So exclusion is not a property of the outcome alone. It requires EVIDENCE THAT NO WORK
#: HAPPENED. A goal stopped before its first turn was mistyped, misfired, or abandoned at the
#: gate -- nothing was attempted and there is nothing to grade. A goal stopped after several
#: turns was attempted; the operator watched it and gave up on it, and that judgement is
#: information about the run, not a reason to delete it from the measurement.
#:
#: The line is drawn at the FIRST TURN rather than at a stopwatch on purpose. Turn count is
#: already recorded for every worker, it is the currency this environment is billed in, and it
#: does not stretch with machine load the way a wall-clock threshold does -- under a heavy
#: fleet, thirty seconds is easily consumed by a mistyped goal that never ran at all.
SCORING = {
    "DONE": "pass",
    # A goal that split still owes an answer -- its family's merge is where that answer comes
    # from. Excluding it does not prevent a double count where the denominator is benchmark
    # instances (one prediction per instance); it deletes the instance.
    "FANOUT": "fail",
    "MAXTURNS": "fail",
    # Both of the outcomes below are excluded ONLY on evidence of no work; see
    # EXCLUDED_WITHOUT_WORK. Listed here as failures because that is the side to land on when
    # the turn count is unknown -- the unknown must not fall toward the answer that flatters.
    "CANCELLED": "fail",
    "CONTENT_REFUSED": "fail",
    "STUCK": "fail",
    "INFRA_STUCK": "fail",
    "REFUSED": "fail",
    "VERIFY_FAILED": "fail",
    # A claim the record does not support does not count as a pass.
    "EVIDENCE_CONTRADICTED": "fail",
    "ERROR": "fail",
}

#: Outcomes that leave the denominator WHEN THE WORKER TOOK NO TURNS, and only then.
#:
#: CANCELLED is a human saying stop. INFRA_STUCK is the outcome invented to mean "our path
#: looked unhealthy" -- the connection or agent never established. Neither describes an agent
#: that tried and failed. But neither guarantees an agent that did not try, either: a run
#: stopped at turn nine was tried, and an INFRA_STUCK after nine turns is a connection that
#: died mid-work, not one that never opened. The turn count is what separates them, and it is
#: the only part of this that cannot be argued with after the fact.
EXCLUDED_WITHOUT_WORK = frozenset({"CANCELLED", "INFRA_STUCK"})


def scoring_of(outcome, turns=None) -> str:
    """'pass' | 'fail' | 'excluded' for `outcome`, given what the worker actually did.

    `turns` is how many turns the worker took. None means "not recorded", and that resolves to
    the scored side, never to 'excluded': an unknown must not fall toward the answer that
    raises the rate, because unknowns are exactly what a broken ledger produces in bulk.

    FAIL-CLOSED BY CONSTRUCTION. The raise below is not defensive politeness: an unlisted
    outcome must not be able to reach 'excluded'. A new outcome fails here on the commit that
    introduces it, which is the moment somebody knows which side it belongs on.
    """
    try:
        side = SCORING[outcome]
    except (KeyError, TypeError):
        raise UnknownOutcome(
            "outcome %r is not in the closed set; add it to SCORING in relay/outcomes.py "
            "with the side it scores on, rather than letting it default" % (outcome,))
    if outcome in EXCLUDED_WITHOUT_WORK and turns is not None:
        try:
            if int(turns) <= 0:
                return "excluded"
        except (TypeError, ValueError):
            return side
    return side


def tally(rows):
    """Count a run's outcomes into the rates that must always be reported together.

    `rows` may be outcome strings, or (outcome, turns) pairs. A bare string means the turn
    count was not recorded, which scores rather than excludes -- see `scoring_of`.

    TWO QUESTIONS, NEVER ONE NUMBER. Every exclusion added to a suite RAISES `conditional`,
    because excluding leaves the denominator; several rounds of honest exclusions look exactly
    like several rounds of improvement, and nothing in a single number tells them apart.

    `conditional` asks what fraction of GRADABLE attempts passed. `end_to_end` asks what
    fraction of everything the caller asked for came back with an answer -- an unhealthy
    environment cannot flatter itself there. `excluded_rate` is the health of the measurement
    itself: a run that excluded most of its work is not a run with a high score, it is a run
    that did not measure anything.
    """
    counts = {"pass": 0, "fail": 0, "excluded": 0}
    for row in rows:
        if isinstance(row, (tuple, list)):
            outcome, turns = (list(row) + [None])[:2]
        else:
            outcome, turns = row, None
        counts[scoring_of(outcome, turns)] += 1
    total = sum(counts.values())
    gradable = counts["pass"] + counts["fail"]
    return {
        "passed": counts["pass"],
        "failed": counts["fail"],
        "excluded": counts["excluded"],
        "gradable": gradable,
        "total": total,
        "conditional": (counts["pass"] / gradable) if gradable else None,
        "end_to_end": (counts["pass"] / total) if total else None,
        "excluded_rate": (counts["excluded"] / total) if total else None,
    }


# The invariants this module exists to hold, checked at import so a bad edit cannot ship.
assert set(STATUS_OF) == OUTCOMES
assert RETRYABLE | NON_RETRYABLE == OUTCOMES and not (RETRYABLE & NON_RETRYABLE)
assert FINISHED <= OUTCOMES
assert set(NOT_PRODUCED) <= OUTCOMES
assert set(SCORING) == OUTCOMES
assert set(SCORING.values()) <= {"pass", "fail", "excluded"}
assert EXCLUDED_WITHOUT_WORK <= OUTCOMES
