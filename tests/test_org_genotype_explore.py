# -*- coding: utf-8 -*-
"""Bounded exploration loop (slice E5): budget, determinism, holdout rejection, CLI."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.org import genotype as G  # noqa: E402
from relay.org import genotype_candidates as C  # noqa: E402
from relay.org import genotype_explore as E  # noqa: E402
from relay.org import genotype_holdout as H  # noqa: E402

SCRIPT = os.path.join(REPO, "scripts", "org_genotype_explore.py")
HOLD = ["item-h1", "item-h2"]
DEV = ["item-d1"]


def _run(parent=None, rounds=2, cands=6, seed=0, evaluate=None, **kw):
    parent = parent or G.current()
    kw.setdefault("sealed", H.seal(HOLD))
    kw.setdefault("dev_ids", DEV)
    kw.setdefault("holdout_ids", HOLD)
    return E.explore(parent, evaluate or E.dry_run_evaluator(), rounds, cands, seed=seed, **kw)


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, None, "3"])
def test_budgets_are_required_positive_integers(bad):
    with pytest.raises(E.ExploreError):
        _run(rounds=bad)
    with pytest.raises(E.ExploreError):
        _run(cands=bad)


def test_budget_caps_candidates_and_runs():
    r = _run(rounds=3, cands=4)
    assert r["generated"] <= 4 and len(r["rounds"]) <= 3
    assert r["evaluated"] <= (3 + 4) * 2
    assert r["budget"] == {"max_rounds": 3, "max_candidates": 4}


def test_deterministic_for_a_seed():
    assert E.to_json(_run(seed=5)) == E.to_json(_run(seed=5))


def test_sealed_pool_is_refused():
    with pytest.raises(E.ExploreError):
        _run(pools=("evolution", "sealed"))


def test_no_improvement_stops_the_loop():
    r = _run(rounds=5, evaluate=lambda genes, pool: {"score": 1.0})
    assert r["stop_reason"] == "no_improvement" and len(r["rounds"]) == 1
    assert r["archive"]["entries"]


def test_improving_evaluator_walks_to_a_new_parent():
    # score grows with the (sorted) hash order of nothing in particular: use a gene-based score
    parent = G.current()
    first = C.generate(parent, 50, seed=1)
    target = first[0]["genotype"]["genes"]
    tid = G.genotype_hash(target, target)
    pid = G.genotype_hash(parent, parent)

    def ev(genes, pool):
        h = G.genotype_hash(genes, genes)
        return {"score": 2.0 if h == tid else 1.0 if h == pid else 0.5}

    r = _run(parent, rounds=2, cands=100, seed=1, evaluate=ev)
    assert r["rounds"][0]["best"] == tid
    ids = [e["id"] for e in r["archive"]["entries"]]
    assert tid in ids


def test_unsealed_holdout_rejects_everything():
    r = _run(sealed=None)
    assert r["rejected_count"] >= 1
    assert r["archive"]["entries"] == []
    assert set(r["rejected_holdout"].values()) == {"holdout unsealed"}


def test_leaked_candidate_is_rejected_and_counted():
    parent = G.current()
    cand = C.generate(parent, 3, seed=0)[0]["genotype"]["id"]
    ex = [{"item": "item-h1", "genotype": cand, "purpose": "tuning", "at": "t"}]
    r = _run(parent, rounds=1, cands=50, seed=0, exposures=ex)
    assert r["rejected_holdout"] == {cand: "leaked: item-h1"}
    assert r["rejected_count"] == 1
    assert cand not in [e["id"] for e in r["archive"]["entries"]]
    assert cand in r["rounds"][0]["rejected_holdout"]


def test_digest_mismatch_rejects_all():
    r = _run(holdout_ids=HOLD + ["item-h9"])
    assert set(r["rejected_holdout"].values()) == {"holdout digest mismatch"}
    assert r["archive"]["entries"] == []


def test_over_budget_candidate_rejected():
    parent = G.current()
    cand = C.generate(parent, 3, seed=0)[0]["genotype"]["id"]
    ex = [{"item": "item-h1", "genotype": cand, "purpose": "evaluation", "at": "t"},
          {"item": "item-h1", "genotype": "other", "purpose": "evaluation", "at": "t"}]
    r = _run(parent, rounds=1, cands=50, seed=0, exposures=ex)
    assert r["rejected_holdout"][cand] == "over budget: item-h1"


def _cli(*args):
    return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True,
                          encoding="utf-8", timeout=120)


def test_cli_writes_only_to_out_and_refuses_unbounded(tmp_path):
    hold = tmp_path / "hold.json"
    hold.write_text(json.dumps(HOLD), encoding="utf-8")
    seal = tmp_path / "seal.json"
    seal.write_text(json.dumps(H.seal(HOLD)), encoding="utf-8")
    out = tmp_path / "report.json"
    p = _cli("--rounds", "2", "--max-candidates", "5", "--holdout", str(hold),
             "--seal", str(seal), "--out", str(out))
    assert p.returncode == 0, p.stderr
    assert "archive front" in p.stdout
    assert json.loads(out.read_text(encoding="utf-8"))["budget"]["max_candidates"] == 5
    assert sorted(os.listdir(tmp_path)) == ["hold.json", "report.json", "seal.json"]
    assert _cli("--max-candidates", "5").returncode == 2  # argparse: --rounds required
    assert _cli("--rounds", "0", "--max-candidates", "5").returncode == 2
    assert _cli("--rounds", "1", "--max-candidates", "1", "--out",
                os.path.join(str(tmp_path), ".fleet", "r.json")).returncode == 2
