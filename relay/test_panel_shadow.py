"""The panel's per-lens record, and the veto aggregator it exists to let us price.

Nothing here changes what the pipeline accepts. These are the properties that make a LATER
decision about the aggregator answerable from runs that already happened -- which is the only
way to know a veto's cost before paying it.
"""
from relay.refuter import (PANEL_LENSES, VETO_LENSES, aggregate_panel,
                           aggregate_panel_veto, panel_shadow)


def test_the_case_the_veto_exists_for():
    """A lone security finding. `correctness` and `edge` did not examine it and cannot
    corroborate it, so the majority reads their silence as agreement and lets it through."""
    r = [("correctness", "UPHELD", ""), ("edge", "UPHELD", ""),
         ("security", "REFUTED", "path traversal in the new handler")]
    assert aggregate_panel(r)[0] == "UPHELD"
    assert aggregate_panel_veto(r)[0] == "REFUTED"
    assert panel_shadow(r)["would_flip"] is True


def test_a_lone_non_veto_objection_is_still_outvoted():
    """The veto changes ONE thing. An over-eager correctness reviewer must still not block --
    that is the property the majority rule was written for, and it is kept."""
    r = [("correctness", "REFUTED", "I would have named this differently"),
         ("edge", "UPHELD", ""), ("security", "UPHELD", "")]
    assert aggregate_panel(r)[0] == "UPHELD"
    assert aggregate_panel_veto(r)[0] == "UPHELD"
    assert panel_shadow(r)["would_flip"] is False


def test_the_two_aggregators_agree_when_the_panel_agrees():
    """No flip means no cost and no benefit. Most runs must land here, or the veto is not a
    refinement but a different policy."""
    for kind in ("UPHELD", "REFUTED"):
        r = [(l, kind, "r" if kind == "REFUTED" else "") for l in PANEL_LENSES]
        assert (aggregate_panel(r)[0] == aggregate_panel_veto(r)[0])
        assert panel_shadow(r)["would_flip"] is False


def test_a_veto_lens_that_upholds_does_not_grant_a_pass():
    """A veto is a power to REFUSE, not a power to approve. Security saying nothing is wrong
    must not override two other lenses that both found something."""
    r = [("correctness", "REFUTED", "wrong branch"), ("edge", "REFUTED", "off by one"),
         ("security", "UPHELD", "")]
    assert aggregate_panel_veto(r)[0] == "REFUTED"


def test_a_veto_lens_that_could_not_be_conducted_does_not_block():
    """UNCLEAR is not REFUSED. A reviewer that never ran has not objected to anything, and
    treating a harness fault as a veto would turn every outage into a work stoppage."""
    r = [("correctness", "UPHELD", ""), ("edge", "UPHELD", ""),
         ("security", "UNCLEAR", "harness: no page")]
    assert aggregate_panel_veto(r)[0] == "UPHELD"


def test_harness_faults_are_named_so_a_lens_that_never_ran_is_not_credited():
    """Scoring a policy against a reviewer that was never asked credits it with a clean result
    it did not obtain."""
    r = [("correctness", "UPHELD", ""), ("edge", "UNCLEAR", "harness: no page"),
         ("security", "UNCLEAR", "I could not decide")]
    rec = panel_shadow(r)
    assert rec["harness_faults"] == ["edge"]


def test_an_empty_panel_is_unclear_under_both_rules():
    assert aggregate_panel_veto([])[0] == "UNCLEAR"
    assert panel_shadow([])["would_flip"] is False


def test_the_record_keeps_every_lens_verbatim():
    """The per-lens reasons are the evidence a human adjudicates when deciding whether a
    would-be veto was a real finding. Summarising them away defeats the purpose."""
    r = [("correctness", "UPHELD", ""), ("edge", "UPHELD", ""),
         ("security", "REFUTED", "unsanitised path joins user input")]
    rec = panel_shadow(r)
    assert [x["lens"] for x in rec["lenses"]] == ["correctness", "edge", "security"]
    assert rec["lenses"][2]["reason"] == "unsanitised path joins user input"


def test_the_recorded_aggregate_is_the_one_the_pipeline_actually_used():
    """If the record and the pipeline could disagree, the record would be measuring a
    different system than the one that ran."""
    r = [("correctness", "REFUTED", "a"), ("edge", "REFUTED", "b"), ("security", "UPHELD", "")]
    assert panel_shadow(r)["aggregate"]["kind"] == aggregate_panel(r)[0]


def test_security_is_the_veto_lens_and_it_is_one_of_the_panel():
    """A veto lens that the panel never runs is a rule with no subject."""
    assert set(VETO_LENSES) <= set(PANEL_LENSES)
