"""The outcome set must be closed, and closed against the code rather than against a list.

An enum kept by hand is a list somebody remembered to update. This walks the AST of the relay
package for every string literal assigned to a worker's `.outcome` and requires it to be a
member -- so an outcome invented in relay_fleet.py fails here on the commit that invents it,
which is the moment somebody knows what status it ought to report.

That is the property the old `if/elif/return "error"` chain could not have. Twice a new
outcome was added and the chain reported it as a failure: INFRA_STUCK and REFUSED, which the
same file already listed as retryable, and FANOUT, so a run whose nine subtasks all completed
and merged reported 0 done of 1.
"""
import ast
import os

import pytest

from relay import outcomes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELAY = os.path.join(ROOT, "relay")


def _assigned_outcomes():
    """Every string literal assigned to something named `outcome`, with where it came from."""
    found = {}
    for name in sorted(os.listdir(RELAY)):
        if not name.endswith(".py") or name.startswith("test_"):
            continue
        path = os.path.join(RELAY, name)
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError:                       # not ours to police here
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            # self.outcome = "X"   and   self.status, self.outcome = "x", "X"
            targets, values = [], []
            for tgt in node.targets:
                if isinstance(tgt, ast.Tuple):
                    targets.extend(tgt.elts)
                    values.extend(node.value.elts
                                  if isinstance(node.value, ast.Tuple) else [])
                else:
                    targets.append(tgt)
                    values.append(node.value)
            for tgt, val in zip(targets, values):
                if not (isinstance(tgt, ast.Attribute) and tgt.attr == "outcome"):
                    continue
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    found.setdefault(val.value, []).append("%s:%d" % (name, node.lineno))
    return found


def test_every_outcome_the_code_assigns_is_a_member():
    assigned = _assigned_outcomes()
    assert assigned, "the walker found nothing -- it has stopped matching the code's shape"
    unlisted = {k: v for k, v in assigned.items() if k not in outcomes.OUTCOMES}
    assert not unlisted, (
        "outcomes assigned in the relay package but absent from relay/outcomes.py: %s.\n"
        "Add each one with the status it should report -- a value that is not a member has no "
        "meaning to any reader." % unlisted)


def test_every_member_is_produced_or_declared_unproduced():
    """The reverse direction: a member nothing emits is either dead or a plumbing gap, and
    both are worth knowing.

    UNRESOLVED_REFUSAL was the first thing this found -- an outcome, a status, a pill and a
    terminal-state entry, five places, for a value no branch anywhere assigned. It was kept for
    a while on the grounds that removing it would change the UI's vocabulary, which was the
    wrong way round: the vocabulary described a state the system cannot reach. All five are
    gone, and NOT_PRODUCED is empty, which is what this asserts against."""
    assigned = set(_assigned_outcomes())
    never = outcomes.OUTCOMES - assigned - set(outcomes.NOT_PRODUCED)
    assert not never, (
        "listed but never assigned, and not declared as such: %s. Either something emits it "
        "and the walker missed it, or it belongs in NOT_PRODUCED with the reason." % sorted(never))
    for name, why in outcomes.NOT_PRODUCED.items():
        assert name not in assigned, (
            "%s is declared unproduced (%r) but the code assigns it" % (name, why))


def test_status_of_raises_rather_than_defaulting():
    with pytest.raises(outcomes.UnknownOutcome):
        outcomes.status_of("SOMETHING_NEW")
    with pytest.raises(outcomes.UnknownOutcome):
        outcomes.status_of(None)


def test_the_two_outcomes_that_were_misreported_now_map_to_their_meaning():
    """The regression this exists for, named. Both of these once returned "error"."""
    assert outcomes.status_of("FANOUT") == "done"
    assert outcomes.status_of("INFRA_STUCK") == "stuck"
    assert outcomes.status_of("REFUSED") == "stuck"


def test_retryable_and_non_retryable_partition_the_set():
    assert outcomes.RETRYABLE | outcomes.NON_RETRYABLE == outcomes.OUTCOMES
    assert not (outcomes.RETRYABLE & outcomes.NON_RETRYABLE)
    with pytest.raises(outcomes.UnknownOutcome):
        outcomes.is_retryable("NOPE")


def test_infra_stuck_is_retryable_but_not_finished():
    """The two sets are NOT complements and the difference is deliberate: INFRA_STUCK says our
    own path looked unhealthy, so a fresh browser context deserves another attempt at the same
    goal -- whereas STUCK means the goal itself is not going to work."""
    assert "INFRA_STUCK" in outcomes.RETRYABLE
    assert "INFRA_STUCK" not in outcomes.FINISHED
    assert "STUCK" in outcomes.FINISHED


def test_every_status_the_mapping_can_return_has_a_pill():
    """A status with no entry in STATUS_PILL renders as nothing in the cockpit."""
    from relay.fleet_runner import STATUS_PILL
    for outcome, status in outcomes.STATUS_OF.items():
        assert status in STATUS_PILL, "%s -> %r has no pill" % (outcome, status)


def test_the_runner_and_the_enum_have_not_drifted_apart():
    from relay import fleet_runner
    assert fleet_runner.RETRYABLE_OUTCOMES is outcomes.RETRYABLE
    assert fleet_runner.NON_RETRYABLE_OUTCOMES is outcomes.NON_RETRYABLE


# ---------------------------------------------------------------------------
# The scoring partition: which outcomes may enter a measured denominator.
# ---------------------------------------------------------------------------


def test_scoring_is_total_over_the_closed_set():
    """Same property as STATUS_OF, for the same reason. A scoring side reached by omission is
    indistinguishable from one nobody considered, and the omissions all fall the same way:
    toward 'excluded', which RAISES the reported rate."""
    from relay.outcomes import SCORING, OUTCOMES
    assert set(SCORING) == OUTCOMES
    assert set(SCORING.values()) <= {"pass", "fail", "excluded"}


def test_an_unlisted_outcome_raises_rather_than_being_excluded():
    """FAIL-CLOSED. The dangerous default here is not 'fail' -- it is 'excluded', because that
    is the one that flatters the score. An outcome nobody has classified must stop the run."""
    from relay.outcomes import scoring_of, UnknownOutcome
    with pytest.raises(UnknownOutcome):
        scoring_of("AN_OUTCOME_NOBODY_HAS_CLASSIFIED")


def test_a_human_stop_is_not_scored_as_a_failure():
    """CANCELLED is a human saying stop. Counting it against the agent measures the operator."""
    from relay.outcomes import scoring_of
    assert scoring_of("CANCELLED") == "excluded"


def test_a_path_that_never_established_is_not_scored_as_a_failure():
    """INFRA_STUCK exists precisely to mean 'our path looked unhealthy'. There was no attempt
    to grade -- this is the misreport the outcome was invented to prevent, in the scoring
    dimension rather than the reporting one."""
    from relay.outcomes import scoring_of
    assert scoring_of("INFRA_STUCK") == "excluded"


def test_a_fanout_parent_is_not_counted_twice():
    """FANOUT is done AS A GOAL, but the answer arrives from the merge that follows its family.
    Scoring the parent as a pass counts one goal in two places; scoring it as a failure is
    worse. It is not a gradable attempt."""
    from relay.outcomes import scoring_of
    assert scoring_of("FANOUT") == "excluded"


def test_the_signals_a_retry_floor_measures_stay_in_the_denominator():
    """REFUSED and MAXTURNS are exactly what a retry floor and an effort router are measured
    against. Excluding them would delete the quantity under study -- an exclusion that looks
    like an improvement."""
    from relay.outcomes import scoring_of
    for outcome in ("REFUSED", "MAXTURNS", "STUCK", "VERIFY_FAILED", "ERROR",
                    "CONTENT_REFUSED"):
        assert scoring_of(outcome) == "fail", outcome


def test_the_excluded_set_is_read_from_the_mapping_not_hand_kept():
    """A second copy of the list is a second thing to forget. Every omission this module was
    written to prevent was an omission from a hand-kept set."""
    from relay.outcomes import SCORING, EXCLUDED_FROM_DENOMINATOR
    assert EXCLUDED_FROM_DENOMINATOR == {k for k, v in SCORING.items() if v == "excluded"}


def test_tally_reports_both_rates_and_the_health_of_the_measurement():
    """TWO QUESTIONS, NEVER ONE NUMBER. `conditional` rises when work is excluded; `end_to_end`
    cannot. Reporting only the first makes an unhealthy environment look like progress."""
    from relay.outcomes import tally
    t = tally(["DONE", "DONE", "STUCK", "CANCELLED", "INFRA_STUCK", "FANOUT"])
    assert t["gradable"] == 3 and t["total"] == 6
    assert t["conditional"] == pytest.approx(2 / 3)
    assert t["end_to_end"] == pytest.approx(2 / 6)
    assert t["excluded_rate"] == pytest.approx(0.5)


def test_excluding_work_raises_the_conditional_rate_but_not_end_to_end():
    """The specific confusion the two rates exist to separate, stated as an executable fact:
    swapping a failure for an exclusion improves `conditional` while `end_to_end` holds."""
    from relay.outcomes import tally
    before = tally(["DONE", "STUCK"])
    after = tally(["DONE", "CANCELLED"])
    assert after["conditional"] > before["conditional"]
    assert after["end_to_end"] == before["end_to_end"]


def test_an_empty_run_reports_no_rate_rather_than_a_perfect_one():
    """Zero gradable attempts is not 100% and not 0%. A division guarded into a number is how
    a run that measured nothing gets reported as a result."""
    from relay.outcomes import tally
    t = tally([])
    assert t["conditional"] is None and t["end_to_end"] is None
