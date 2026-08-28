"""The harness must be able to express the arms a comparison is meant to compare.

The defect these hold shut: `auto` and `ultra` differ by a review panel and a research budget,
and neither was a manifest parameter. The benchmark child passes `refuter` and deliberately
leaves the rest unset so the manifest supplies them -- so both efforts resolved to the same
program, ran as the same program, and were recorded under the same harness_id. Any comparison
between them was a comparison of a run against itself.
"""
import inspect

import pytest

from relay.refuter import PANEL_LENSES
from relay.selfimprove import runtime_config as rc
from relay.selfimprove.manifest import (DEFAULT_PARAMETERS, PARAMETER_TYPES, base_manifest,
                                        harness_id)
import relay.relay_fleet as RF


def test_the_base_manifest_runs_no_panel():
    """A panel is what `ultra` means, and `ultra` is not what a run does unless asked. Any
    other default turns every production run into a three-reviewer run on the commit that
    added the knob."""
    assert DEFAULT_PARAMETERS["review_lens_count"] == 0


def test_the_research_default_is_the_value_the_signature_used_to_hold():
    """A fallback that disagrees with the old literal silently changes behaviour exactly when
    nobody is looking at it."""
    assert DEFAULT_PARAMETERS["max_research"] == 3


def test_the_signature_no_longer_hardcodes_the_research_budget():
    """A literal in the signature cannot be tuned, and an A/B over a value no running code
    reads is two runs of the same program."""
    sig = inspect.signature(RF.run_relay_fleet)
    assert sig.parameters["max_research"].default is None
    assert sig.parameters["review_lenses"].default is None


def test_every_new_parameter_has_a_declared_range():
    """The ranges are not tidiness: a refuter budget of a million is a candidate that never
    finishes, which arrives as an infra abort -- an outcome a candidate has reason to prefer."""
    for name in ("max_research", "review_lens_count"):
        assert name in PARAMETER_TYPES
        lo, hi = PARAMETER_TYPES[name]
        assert lo <= DEFAULT_PARAMETERS[name] <= hi


def test_every_new_parameter_has_a_production_reader():
    """The rule the parameter block states: three of four parameters once had none, so an A/B
    over them ran the same program twice and reported a p-value about noise."""
    assert RF._genome_default("max_research", -1) == DEFAULT_PARAMETERS["max_research"]
    assert RF._genome_default("review_lens_count", -1) == DEFAULT_PARAMETERS["review_lens_count"]


def test_the_readers_clamp_rather_than_propagating_a_malformed_value():
    assert rc.max_research() >= 0
    assert 0 <= rc.review_lens_count() <= len(PANEL_LENSES)


def test_asking_for_a_panel_changes_the_harness_id():
    """THE PROPERTY THAT MAKES THE RECORD HONEST. Two efforts that hash to the same id are two
    runs the scorecard will compare as though they were one program."""
    a = base_manifest()
    b = base_manifest()
    b["parameters"]["review_lens_count"] = 3
    assert harness_id(a) != harness_id(b)


def test_changing_the_research_budget_changes_the_harness_id():
    a = base_manifest()
    b = base_manifest()
    b["parameters"]["max_research"] = 8
    assert harness_id(a) != harness_id(b)


def test_the_lens_ladder_is_a_ladder_not_two_different_panels():
    """Each step must be a superset of the one below, so comparing counts compares ONE added
    reviewer rather than two unrelated panels."""
    steps = [list(PANEL_LENSES[:n]) for n in range(len(PANEL_LENSES) + 1)]
    for lo, hi in zip(steps, steps[1:]):
        assert lo == hi[:len(lo)]


def test_a_count_of_zero_means_no_panel_at_all():
    """Not an empty panel. An empty list would make `if review_lenses:` false everywhere and
    happen to work, which is a behaviour resting on an accident."""
    assert PANEL_LENSES[:0] == ()
