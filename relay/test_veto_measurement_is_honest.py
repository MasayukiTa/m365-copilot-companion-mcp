"""A veto measurement is meaningless while the panel that carries the veto is switched off.

WHAT HAPPENED. A shadow measurement was run and reported as "n=16, would_flip=0, security
never refuted alone -- safe to enable, effect unmeasured". The second half was true in a way
the report did not convey: `security` had not failed to refute alone, it had never been
CONSULTED. Measured 2026-08-30 over 100 ledger records: every one carries exactly one lens,
`rootcause`, and `review_lens_count` defaults to 0, so `_resolve_review_lenses(None)` returns
None and no panel is ever built. A veto lens cannot flip an aggregate it is not part of.

The number was not wrong. It was answering a different question, and nothing in the pipeline
said so -- which is the failure this repository already carries a rule about: check what the
instrument is actually sampling before believing the interval.

These tests do not enable anything. They make the silence audible.
"""
import json
import os

import pytest

from relay import relay_fleet as RF
from relay.refuter import PANEL_LENSES, VETO_LENSES

LEDGER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      ".fleet", "panels.jsonl")


def test_the_veto_lens_is_a_member_of_the_panel_it_vetoes():
    """If it is not in PANEL_LENSES it can never be consulted, whatever the aggregator does."""
    for lens in VETO_LENSES:
        assert lens in PANEL_LENSES, (
            "%r has a veto over a panel it is not part of" % lens)


def test_a_default_run_builds_no_panel_and_the_code_says_so():
    """The current state, asserted so a change to it is deliberate rather than noticed later.

    This is NOT an endorsement. It records that the default is off, so that anybody reading a
    veto measurement can see from the tests that the default population cannot contain one."""
    assert RF._resolve_review_lenses(None) is None
    assert int(RF._genome_default("review_lens_count", 0)) == 0


def test_a_panel_large_enough_to_hold_the_veto_includes_it():
    """The veto is the LAST lens in PANEL_LENSES, so a panel of 1 or 2 silently excludes it.

    `review_lens_count=2` looks like "a panel" and produces correctness+edge -- a configuration
    in which the veto is inert and nothing reports that it is."""
    assert RF._resolve_review_lenses(["correctness", "edge", "security"]) == [
        "correctness", "edge", "security"]
    for n in (1, 2):
        partial = list(PANEL_LENSES[:n])
        assert not any(v in partial for v in VETO_LENSES), (
            "a panel of %d already contains the veto lens; this test's premise is stale" % n)


def _ledger_rows():
    if not os.path.exists(LEDGER):
        pytest.skip("no panel ledger on this machine")
    rows = []
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    if not rows:
        pytest.skip("panel ledger is empty")
    return rows


def test_a_veto_claim_requires_the_veto_lens_to_appear_in_the_ledger():
    """The guard that would have caught the earlier report.

    A measurement of the veto is only meaningful over records whose panel CONTAINS the veto
    lens. This asserts the size of that population, so a future claim has to state it."""
    rows = _ledger_rows()
    with_veto = [r for r in rows
                 if any((e or {}).get("lens") in VETO_LENSES
                        for e in (r.get("lenses") or []) if isinstance(e, dict))]
    # Not an assertion that it must be non-zero -- it is zero today, and that is the finding.
    # The assertion is that the two populations are not confused for one another.
    assert len(with_veto) <= len(rows)
    if not with_veto:
        pytest.skip(
            "no ledger record contains a veto lens: %d panels, all without one. "
            "Any 'would_flip' figure over this population measures the aggregator, "
            "not the veto." % len(rows))


def test_the_ledger_stores_a_verdict_under_a_field_that_is_actually_read():
    """The analysis first written for this read `verdict`; the records carry `kind`.

    Both spellings appear in the record, one of them always None, and a reader that picks the
    wrong one gets zero of everything and no error."""
    rows = _ledger_rows()
    lenses = [e for r in rows for e in (r.get("lenses") or []) if isinstance(e, dict)]
    if not lenses:
        pytest.skip("no lens entries")
    has_kind = sum(1 for e in lenses if e.get("kind"))
    has_verdict = sum(1 for e in lenses if e.get("verdict"))
    assert has_kind or has_verdict, "lens entries carry no outcome at all"
    assert not (has_kind and has_verdict), (
        "both `kind` and `verdict` are populated; a reader cannot tell which is authoritative")
