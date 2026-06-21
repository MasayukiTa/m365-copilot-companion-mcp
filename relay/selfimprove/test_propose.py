"""Unit tests for the PROPOSE step. Run: python -m relay.selfimprove.test_propose"""
import os
import tempfile

from relay.selfimprove import propose as P
from relay.selfimprove.archive import Archive, genome_id


def _clean_card(text):
    """A domain-general card that passes overfit_lint."""
    return {"knobs": {}, "cards": {"c": text}, "parent_id": None, "note": ""}


def test_clean_novel_candidate_survives():
    with tempfile.TemporaryDirectory() as d:
        arc = Archive(os.path.join(d, "entries.jsonl"))   # empty archive -> parent None
        cand = _clean_card("verify the exact expected output, not just that it runs")

        out = P.propose_candidates(
            real_misses=[],
            archive=arc,
            generate_fn=lambda misses, parent, n: [cand],
            n=3,
        )
        assert len(out) == 1
        assert genome_id(out[0]) == genome_id(cand)
        assert out[0]["parent_id"] is None                # empty archive -> no parent stamped
    print("ok test_clean_novel_candidate_survives")


def test_overfit_candidate_dropped():
    with tempfile.TemporaryDirectory() as d:
        arc = Archive(os.path.join(d, "entries.jsonl"))
        good = _clean_card("trace the symptom through the code path before editing")
        # names a repo + instance + source file + test -> overfit_lint fires
        bad = _clean_card("in django__django-12345 patch django/forms/widgets.py and test_merge")

        assert P.lint_candidate(bad)                       # the linter sees it
        assert P.lint_candidate(good) == []

        out = P.propose_candidates(
            real_misses=[],
            archive=arc,
            generate_fn=lambda misses, parent, n: [bad, good],
            n=3,
        )
        ids = [genome_id(g) for g in out]
        assert genome_id(good) in ids and genome_id(bad) not in ids
        assert len(out) == 1
    print("ok test_overfit_candidate_dropped")


def test_rejected_id_dropped():
    with tempfile.TemporaryDirectory() as d:
        arc = Archive(os.path.join(d, "entries.jsonl"))
        dead = _clean_card("a previously-rejected dead idea")
        live = _clean_card("a fresh domain-general idea")

        out = P.propose_candidates(
            real_misses=[],
            archive=arc,
            generate_fn=lambda misses, parent, n: [dead, live],
            rejected_ids={genome_id(dead)},
            n=3,
        )
        ids = [genome_id(g) for g in out]
        assert genome_id(dead) not in ids and genome_id(live) in ids
        assert len(out) == 1
    print("ok test_rejected_id_dropped")


def test_already_in_archive_dropped():
    with tempfile.TemporaryDirectory() as d:
        arc = Archive(os.path.join(d, "entries.jsonl"))
        seen = _clean_card("an idea already validated and archived")
        arc.add(seen, slice_ids=["i1"], pass_at_1=0.5)     # now present in the archive
        fresh = _clean_card("a never-tried domain-general idea")

        out = P.propose_candidates(
            real_misses=[],
            archive=arc,
            generate_fn=lambda misses, parent, n: [seen, fresh],
            n=3,
        )
        ids = [genome_id(g) for g in out]
        assert genome_id(seen) not in ids and genome_id(fresh) in ids
        assert len(out) == 1
    print("ok test_already_in_archive_dropped")


def test_noop_equal_to_parent_dropped():
    with tempfile.TemporaryDirectory() as d:
        arc = Archive(os.path.join(d, "entries.jsonl"))
        # seed a parent so select_parent returns it; descriptors so qd has a cell to pick
        parent = {"knobs": {"SS_SELFTEST": "1"}, "cards": {"c": "verify exact output"},
                  "parent_id": None, "note": "seed"}
        arc.add(parent, slice_ids=["i1"], pass_at_1=0.7,
                descriptors={"diff_bin": "surgical", "turns_bin": "short", "dominant_miss": "precision"})

        # generator returns an identical-content genome (same knobs+cards) -> no-op mutation
        noop = {"knobs": {"SS_SELFTEST": "1"}, "cards": {"c": "verify exact output"},
                "parent_id": None, "note": "noop"}
        real_mut = _clean_card("a genuinely different domain-general card")

        assert genome_id(noop) == genome_id(parent)        # identity confirmed

        out = P.propose_candidates(
            real_misses=[],
            archive=arc,
            generate_fn=lambda misses, p, n: [noop, real_mut],
            parent_strategy="qd",
            n=3,
        )
        ids = [genome_id(g) for g in out]
        assert genome_id(parent) not in ids                # the no-op is dropped
        assert genome_id(real_mut) in ids
        # the survivor is stamped with the parent's lineage
        assert out[0]["parent_id"] == genome_id(parent)
    print("ok test_noop_equal_to_parent_dropped")


def test_mutation_generator_clean_and_deterministic():
    # all output is domain-general (passes overfit_lint) for parent None and for a non-None parent
    g1 = P.mutation_generator([], None, 4)
    assert len(g1) == 4
    for cand in g1:
        assert P.lint_candidate(cand) == []                # no repo/instance/file/test leakage

    parent = {"knobs": {"SS_BASE": "1"}, "cards": {"base": "keep changes minimal"},
              "parent_id": None, "note": ""}
    gp = P.mutation_generator([], parent, 3)
    for cand in gp:
        assert P.lint_candidate(cand) == []
        assert cand["knobs"].get("SS_BASE") == "1"         # inherits the parent's knobs
        assert "base" in cand["cards"]                     # inherits the parent's cards

    # deterministic: two calls with the same args yield identical genome ids
    a = [genome_id(c) for c in P.mutation_generator([], parent, 4)]
    b = [genome_id(c) for c in P.mutation_generator([], parent, 4)]
    assert a == b

    # and it survives the full discipline filter against an empty archive
    with tempfile.TemporaryDirectory() as d:
        arc = Archive(os.path.join(d, "entries.jsonl"))
        out = P.propose_candidates([], arc, P.mutation_generator, n=4)
        assert len(out) == 4
    print("ok test_mutation_generator_clean_and_deterministic")


if __name__ == "__main__":
    test_clean_novel_candidate_survives()
    test_overfit_candidate_dropped()
    test_rejected_id_dropped()
    test_already_in_archive_dropped()
    test_noop_equal_to_parent_dropped()
    test_mutation_generator_clean_and_deterministic()
    print("ALL PROPOSE TESTS PASSED")
