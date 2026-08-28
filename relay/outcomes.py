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


#: Whether an outcome counts toward a measured pass rate, and on which side.
#:
#: WHY A THIRD VALUE. Scoring code kept asking one question -- "is this DONE?" -- and every
#: outcome that was neither a pass nor a failure of the system under test got silently filed
#: as a failure. Two of those are not failures at all:
#:
#:   * CANCELLED is a human saying stop. The agent did not fail; it was not allowed to
#:     finish. Counting it as a failure measures the operator, not the agent.
#:   * INFRA_STUCK is the outcome invented precisely to mean "our path looked unhealthy" --
#:     the connection or agent never established. There was no attempt to grade.
#:
#: FANOUT WAS ALSO EXCLUDED HERE, AND THAT WAS WRONG. The argument was that a fan-out parent
#: is done AS A GOAL while its answer comes from the merge, so scoring the parent counts one
#: goal twice. That is true where the denominator is worker rows. It is false where the
#: denominator is BENCHMARK INSTANCES: there is one prediction per instance, so excluding a
#: fan-out parent does not prevent a double count -- it deletes the instance from the
#: benchmark. A parent that split and whose family merged has an answer, and that answer is
#: what the instance's patch contains; if no merge result exists, the instance failed to
#: deliver, which is a failure and not an exclusion. Reported by an external review, verified
#: against the scoring path.
#:
#: Everything else stays a failure ON PURPOSE. REFUSED and MAXTURNS are the signals a retry
#: floor and an effort router exist to act on; moving them out of the denominator would
#: delete the very quantity those mechanisms are measured against.
SCORING = {
    "DONE": "pass",
    # A goal that split still owes an answer; see the note above on why this is not an
    # exclusion. It is scored on whether the family delivered one.
    "FANOUT": "fail",
    "MAXTURNS": "fail",
    "CANCELLED": "excluded",
    "CONTENT_REFUSED": "fail",
    "STUCK": "fail",
    "INFRA_STUCK": "excluded",
    "REFUSED": "fail",
    "VERIFY_FAILED": "fail",
    "ERROR": "fail",
}

#: Excluded from the denominator. Read from SCORING rather than hand-listed: a second copy of
#: this set is a second thing to forget, and the omissions this module exists to prevent were
#: all omissions from a hand-kept list.
EXCLUDED_FROM_DENOMINATOR = frozenset(k for k, v in SCORING.items() if v == "excluded")


def scoring_of(outcome) -> str:
    """'pass' | 'fail' | 'excluded' for `outcome`. Raises rather than guessing.

    FAIL-CLOSED BY CONSTRUCTION. The raise is not defensive politeness: an unlisted outcome
    must not be able to fall into 'excluded', because that is the direction that quietly
    RAISES a reported pass rate. A new outcome fails here on the commit that introduces it,
    which is the moment somebody knows which side it belongs on.
    """
    try:
        return SCORING[outcome]
    except (KeyError, TypeError):
        raise UnknownOutcome(
            "outcome %r is not in the closed set; add it to SCORING in relay/outcomes.py "
            "with the side it scores on, rather than letting it default" % (outcome,))


def tally(outcomes):
    """Count a run's outcomes into the two rates that must always be reported together.

    TWO QUESTIONS, NEVER ONE NUMBER -- the same discipline the companion benchmark already
    holds. Every exclusion added to a suite RAISES `conditional`, because excluding leaves the
    denominator; three rounds of honest exclusions in the same direction look exactly like
    three rounds of improvement, and nothing in a single number tells them apart.

    `conditional` asks what fraction of GRADABLE attempts passed. `end_to_end` asks what
    fraction of everything the caller asked for came back with an answer -- an unhealthy
    environment cannot flatter itself there. `excluded_rate` is the health of the measurement
    itself: a run that excluded most of its work is not a run with a high score, it is a run
    that did not measure anything.
    """
    counts = {"pass": 0, "fail": 0, "excluded": 0}
    for o in outcomes:
        counts[scoring_of(o)] += 1
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
