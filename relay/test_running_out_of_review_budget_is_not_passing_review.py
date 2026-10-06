# -*- coding: utf-8 -*-
"""A run whose last verdict was REFUTED must not be recorded as DONE.

The refuter runs only while `refute_count < max_refute`. A worker refuted on its last
allowed round is sent back to fix the work, and the fix it returns arrives with the budget
spent -- so the gate is not entered, nothing examines the fix, execution falls through to
_settle_done, and the run is recorded DONE.

MEASURED 2026-09-15. A worker could not read a file because the execution tools were locked.
It wrote into the deliverable that it had verified the content against that file. The refuter
caught the fabrication and said so in as many words. The ledger row reads:

    outcome=DONE  turns=10  reason=refuter#3: REFUTED: ... 虚偽の確認済み表記に書き換えており ...

So the system HAD a detector, the detector WORKED, and the result was filed as a success. The
operator's phrase for the class was 「無視して回すだけ回す」.

The outcome used is EVIDENCE_CONTRADICTED, which already existed and already means this: the
claim is contradicted by the record of what was done. Its entry in relay/outcomes.py also
explains why it reports "done" rather than "stuck" -- the work finished, and telling an
operator to re-run something that ran to completion is the wrong instruction. What is in
doubt is the claim.
"""
import pytest

from relay import outcomes
from relay.relay_fleet import RelayWorker


def _worker(**kw):
    w = RelayWorker("do the thing", "w0")
    for k, v in kw.items():
        setattr(w, k, v)
    return w


def test_a_refuted_verdict_with_no_budget_left_is_not_done():
    """The incident: refuted on the last round, fix unexamined, filed as a success."""
    w = _worker(refuter=True, max_refute=3, refute_count=3, _last_refute_verdict="REFUTED")
    assert w._claim_verdict() == "EVIDENCE_CONTRADICTED"


def test_a_refuted_verdict_with_budget_left_is_not_this_test_s_business():
    """With budget remaining the worker is sent back, so _claim_verdict is not reached.

    Asserted anyway: if some future path did settle here mid-review, calling it contradicted
    would be wrong -- nothing has finished being checked yet.
    """
    w = _worker(refuter=True, max_refute=3, refute_count=1, _last_refute_verdict="REFUTED")
    assert w._claim_verdict() == "DONE"


def test_an_upheld_verdict_after_a_refuted_one_clears_it():
    """The work was fixed and approved. Remembering only the refutation would libel it.

    This is why the verdict is recorded for EVERY round rather than inside the REFUTED
    branch: a flag set only on refutation stays set after the refutation is satisfied.
    """
    w = _worker(refuter=True, max_refute=3, refute_count=3, _last_refute_verdict="UPHELD")
    assert w._claim_verdict() == "DONE"


def test_a_run_with_no_refuter_at_all_is_untouched():
    """Most fleet goals run without one; none of them may be demoted by its absence."""
    w = _worker(refuter=False, max_refute=3, refute_count=0)
    assert w._claim_verdict() == "DONE"


def test_the_outcome_is_one_the_taxonomy_already_closes_over():
    """No new outcome. relay/outcomes.py raises on anything unlisted, by design."""
    assert "EVIDENCE_CONTRADICTED" in outcomes.OUTCOMES
    assert outcomes.status_of("EVIDENCE_CONTRADICTED") == "done"


def test_the_refusers_own_words_survive_to_the_operator():
    """A label is not a reason. The operator has to read WHY it is doubted.

    self.reason is written on every verdict and not cleared afterwards, so the last one --
    the refutation -- is what a reader sees. The ledger row from the incident proves this
    already holds; the test keeps it holding.
    """
    w = _worker(refuter=True, max_refute=3, refute_count=3, _last_refute_verdict="REFUTED")
    w.reason = "refuter#3: REFUTED: 本文XMLを読めていないのに確認済みと書いている"
    assert w._claim_verdict() == "EVIDENCE_CONTRADICTED"
    assert "REFUTED" in w.reason and len(w.reason) > 20


@pytest.mark.parametrize("count,budget,expect", [
    (3, 3, "EVIDENCE_CONTRADICTED"),      # spent
    (4, 3, "EVIDENCE_CONTRADICTED"),      # over, if a path ever allowed it
    (2, 3, "DONE"),                       # room remains
])
def test_the_boundary_is_the_budget_being_spent(count, budget, expect):
    w = _worker(refuter=True, max_refute=budget, refute_count=count,
                _last_refute_verdict="REFUTED")
    assert w._claim_verdict() == expect
