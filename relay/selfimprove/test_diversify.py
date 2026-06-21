"""Unit tests for the N-diversity genome generator. Run: python -m relay.selfimprove.test_diversify

Hermetic: no model, no network, no time/random. Deterministic ids -- mutation_generator is
deterministic, so the variant ids are fixed and can be asserted on directly.
"""
import os
import tempfile

from relay.selfimprove import diversify as D
from relay.selfimprove.archive import Archive, genome_id

# The empty base used throughout. Its id and the ids of mutation_generator's variants are fixed.
_BASE = {"knobs": {}, "cards": {}, "parent_id": None, "note": "base"}
_BASE_ID = genome_id(_BASE)
# The genome_id mutation_generator emits for variant index 0 off the empty base (SS_SELFTEST +
# trace_symptom card). Pinned so the rejected/archive exclusion tests are deterministic.
_VARIANT0_ID = "5d03d96f59f4"


def test_diversify_four_distinct_domain_general():
    out = D.diversify(_BASE, 4)
    assert len(out) == 4                                  # 4 attempts for best-of-4
    assert out[0] == _BASE                                # attempt 0 is the incumbent base, verbatim
    ids = [genome_id(g) for g in out]
    assert len(set(ids)) == 4                             # all 4 distinct genome_ids
    assert ids[0] == _BASE_ID
    for g in out:                                         # every attempt is domain-general
        assert D._is_domain_general(g)
    # the variant ids match the pinned deterministic generator output
    assert ids[1] == _VARIANT0_ID
    print("ok test_diversify_four_distinct_domain_general")


def test_n_le_one_is_single_shot():
    assert D.diversify(_BASE, 1) == [_BASE]               # n=1 -> single-shot
    assert D.diversify(_BASE, 0) == [_BASE]               # n=0 -> never empty, still [base]
    assert D.diversify(_BASE, -5) == [_BASE]              # defensive: negative -> [base]
    print("ok test_n_le_one_is_single_shot")


def test_base_none_uses_empty_base():
    out = D.diversify(None, 3)                            # None -> empty base
    assert out[0] == {"knobs": {}, "cards": {}, "parent_id": None, "note": "base"}
    assert len(out) == 3
    assert len({genome_id(g) for g in out}) == 3
    print("ok test_base_none_uses_empty_base")


def test_rejected_ids_excludes_variant():
    # Without rejection, variant index 0 (id _VARIANT0_ID) appears as attempt 1.
    base_ids = {genome_id(g) for g in D.diversify(_BASE, 4)}
    assert _VARIANT0_ID in base_ids
    # Reject it -> it must NOT appear; the slot is filled by the next distinct variant instead.
    out = D.diversify(_BASE, 4, rejected_ids={_VARIANT0_ID})
    ids = [genome_id(g) for g in out]
    assert _VARIANT0_ID not in ids
    assert ids[0] == _BASE_ID                             # base still first
    assert len(set(ids)) == len(ids)                      # still all distinct
    print("ok test_rejected_ids_excludes_variant")


def test_archive_excludes_already_tried_variant():
    with tempfile.TemporaryDirectory() as d:
        arc = Archive(os.path.join(d, "entries.jsonl"))
        # Add the exact genome the generator would emit as variant 0, so its id is already-tried.
        variant0 = {
            "knobs": {"SS_SELFTEST": "1"},
            "cards": {"trace_symptom":
                      "Trace the reported symptom through the code path before editing; change the "
                      "smallest scope that removes the cause."},
            "parent_id": None,
            "note": "pre-tried",
        }
        assert genome_id(variant0) == _VARIANT0_ID         # sanity: it really is variant 0's id
        arc.add(variant0, slice_ids=["x"], pass_at_1=0.5)
        out = D.diversify(_BASE, 4, archive=arc)
        ids = [genome_id(g) for g in out]
        assert _VARIANT0_ID not in ids                     # already tried -> excluded
        assert ids[0] == _BASE_ID
        assert len(set(ids)) == len(ids)
    print("ok test_archive_excludes_already_tried_variant")


def test_diversity_report_clean_run():
    out = D.diversify(_BASE, 4)
    rep = D.diversity_report(out)
    assert rep["n"] == 4
    assert rep["distinct_ids"] == 4                        # distinct_ids == n for a clean run
    assert rep["all_domain_general"] is True
    assert rep["ids"] == [genome_id(g) for g in out]
    assert rep["ids"][0] == _BASE_ID
    print("ok test_diversity_report_clean_run")


if __name__ == "__main__":
    test_diversify_four_distinct_domain_general()
    test_n_le_one_is_single_shot()
    test_base_none_uses_empty_base()
    test_rejected_ids_excludes_variant()
    test_archive_excludes_already_tried_variant()
    test_diversity_report_clean_run()
    print("ALL DIVERSIFY TESTS PASSED")
