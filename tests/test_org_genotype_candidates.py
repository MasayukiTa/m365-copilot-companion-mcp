# -*- coding: utf-8 -*-
"""Candidate generation (slice E2): single-gene, allowed values only, deterministic, read-only."""
from __future__ import annotations

import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.org import genotype as G  # noqa: E402
from relay.org import genotype_candidates as C  # noqa: E402

SCRIPT = os.path.join(REPO, "scripts", "org_genotype_candidates.py")


def _parent():
    return G.current()


def test_every_candidate_changes_exactly_one_gene_to_an_allowed_value():
    parent = _parent()
    cands = C.generate(parent, 50, seed=1)
    assert cands
    for c in cands:
        genes = c["genotype"]["genes"]
        G.validate(genes)
        assert list(G.diff(parent, genes, parent)) == [c["mutation"]["gene"]]
        assert genes[c["mutation"]["gene"]] == c["mutation"]["to"] != c["mutation"]["from"]
        assert c["genotype"]["parent"] == c["parent"] != ""
        assert c["description"]


def test_candidates_are_distinct_programs_and_not_the_parent():
    parent = _parent()
    ids = [c["genotype"]["id"] for c in C.generate(parent, 50, seed=2)]
    assert len(ids) == len(set(ids))
    assert G.genotype_hash(parent, parent) not in ids


def test_deterministic_for_a_seed_and_seed_changes_order():
    parent = _parent()
    a = C.generate(parent, 5, seed=3)
    assert a == C.generate(parent, 5, seed=3)
    assert [c["genotype"]["id"] for c in a] != [c["genotype"]["id"] for c in C.generate(parent, 5, seed=4)]


def test_n_is_respected_and_never_padded():
    parent = _parent()
    total = len(C.generate(parent, 10 ** 6, seed=0))
    assert total == len(C.single_part_moves(parent))
    assert len(C.generate(parent, 3, seed=0)) == 3
    assert C.generate(parent, 0) == [] and C.generate(parent, "x") == []


def test_an_invalid_parent_is_refused():
    bad = _parent()
    bad["effort"] = "not-a-level"
    try:
        C.generate(bad, 3)
    except G.GenotypeError:
        return
    raise AssertionError("invalid parent accepted")


def test_cli_prints_json_and_writes_nothing(tmp_path):
    before = set(os.listdir(os.path.join(REPO, ".fleet"))) if os.path.isdir(os.path.join(REPO, ".fleet")) else None
    out = subprocess.run([sys.executable, "-I", "-X", "utf8", SCRIPT, "-n", "3", "--seed", "1",
                          "--format", "json"], capture_output=True, check=True,
                         cwd=str(tmp_path)).stdout.decode("utf-8")
    import json
    assert len(json.loads(out)) == 3
    after = set(os.listdir(os.path.join(REPO, ".fleet"))) if os.path.isdir(os.path.join(REPO, ".fleet")) else None
    assert before == after
    assert os.listdir(str(tmp_path)) == []
