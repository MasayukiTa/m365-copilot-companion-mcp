# -*- coding: utf-8 -*-
"""Transfer test (slice E6): budget, verdict rules, axes, holdout exclusion, determinism, CLI."""
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
from relay.org import genotype_holdout as H  # noqa: E402
from relay.org import genotype_transfer as T  # noqa: E402

SCRIPT = os.path.join(REPO, "scripts", "org_genotype_transfer.py")
HOLD = ["item-h1", "item-h2"]
DEV = ["item-d1"]
CTX = [{"axis": "model", "name": "m-home", "home": True},
       {"axis": "model", "name": "m-other"},
       {"axis": "kind", "name": "k-one"},
       {"axis": "kind", "name": "k-two"}]


def _run(cands, evaluate, contexts=CTX, parent=None, mc=4, mk=10, **kw):
    kw.setdefault("sealed", H.seal(HOLD))
    kw.setdefault("dev_ids", DEV)
    kw.setdefault("holdout_ids", HOLD)
    return T.transfer(parent or G.current(), cands, contexts, evaluate, mc, mk, **kw)


def _cands(n=1, seed=0):
    return C.generate(G.current(), n, seed=seed)


def _scripted(cand_id, deltas, base=1.0):
    """Parent scores `base` everywhere; the named candidate scores base + deltas[context]."""
    def ev(genes, context):
        h = G.genotype_hash(genes, genes)
        return {"score": base + deltas[context] if h == cand_id else base}
    return ev


def _verdict(deltas, **kw):
    cand = _cands(1)
    cid = cand[0]["genotype"]["id"]
    r = _run(cand, _scripted(cid, deltas), **kw)
    return r["results"][0]


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, None, "3"])
def test_budgets_are_required_positive_integers(bad):
    with pytest.raises(T.TransferError):
        _run(_cands(), T.dry_run_evaluator(), mc=bad)
    with pytest.raises(T.TransferError):
        _run(_cands(), T.dry_run_evaluator(), mk=bad)


def test_budget_caps_evaluator_calls():
    calls = []

    def ev(genes, context):
        calls.append(context)
        return {"score": 0.5}

    r = _run(_cands(8), ev, mc=4, mk=3)
    assert len(calls) == r["evaluated"] <= (3 + 1) * 4
    assert len(r["results"]) == 3 and r["cut"] == 5
    assert r["budget"] == {"max_contexts": 4, "max_candidates": 3}


def test_too_many_contexts_is_refused_not_cut():
    with pytest.raises(T.TransferError):
        _run(_cands(), T.dry_run_evaluator(), mc=3)


def test_context_validation():
    ev = T.dry_run_evaluator()
    with pytest.raises(T.TransferError):
        _run(_cands(), ev, contexts=[{"axis": "model", "name": "sealed", "home": True}])
    with pytest.raises(T.TransferError):
        _run(_cands(), ev, contexts=[{"axis": "size", "name": "a", "home": True}])
    with pytest.raises(T.TransferError):  # no home
        _run(_cands(), ev, contexts=[{"axis": "model", "name": "a"}])
    with pytest.raises(T.TransferError):  # duplicate name
        _run(_cands(), ev, contexts=[{"axis": "model", "name": "a", "home": True},
                                     {"axis": "kind", "name": "a"}])
    with pytest.raises(T.TransferError):
        _run(_cands(), ev, contexts=[])


def test_transfers_when_home_and_most_others_win():
    r = _verdict({"m-home": 0.3, "m-other": 0.1, "k-one": 0.2, "k-two": 0.0})
    assert r["verdict"] == "transfers"  # 2 of 3 others won, none lost
    assert r["axes"]["model"]["wins"] == 2 and r["axes"]["kind"]["wins"] == 1


def test_home_only_when_too_few_others_win():
    r = _verdict({"m-home": 0.3, "m-other": 0.0, "k-one": 0.0, "k-two": 0.1})
    assert r["verdict"] == "home_only"


def test_regresses_beyond_tolerance_but_not_within():
    d = {"m-home": 0.3, "m-other": 0.2, "k-one": 0.2, "k-two": -0.05}
    assert _verdict(d)["verdict"] == "regresses"
    assert _verdict(d, tolerance=0.1)["verdict"] == "transfers"


def test_no_home_gain_is_insufficient():
    r = _verdict({"m-home": 0.0, "m-other": 0.2, "k-one": 0.2, "k-two": 0.2})
    assert r["verdict"] == "insufficient" and r["reason"] == "no home gain"


def test_min_gain_raises_the_bar():
    d = {"m-home": 0.3, "m-other": 0.05, "k-one": 0.05, "k-two": 0.05}
    assert _verdict(d)["verdict"] == "transfers"
    assert _verdict(d, min_gain=0.1)["verdict"] == "home_only"  # others no longer clear the bar
    assert _verdict(d, min_gain=0.4)["verdict"] == "insufficient"  # nor does home


def test_evaluator_failure_is_not_a_loss():
    cand = _cands(1)
    cid = cand[0]["genotype"]["id"]

    def ev(genes, context):
        if context == "k-two":
            raise RuntimeError("harness down")
        return {"score": 1.5 if G.genotype_hash(genes, genes) == cid else 1.0}

    r = _run(cand, ev)["results"][0]
    assert r["deltas"]["k-two"] is None and r["verdict"] == "transfers"

    def bad_home(genes, context):
        return {"score": None} if context == "m-home" else {"score": 1.0}

    assert _run(cand, bad_home)["results"][0]["verdict"] == "insufficient"


def test_only_home_measured_is_insufficient():
    ctx = [{"axis": "model", "name": "a", "home": True}, {"axis": "kind", "name": "b"}]
    cand = _cands(1)
    cid = cand[0]["genotype"]["id"]

    def ev(genes, context):
        if context == "b":
            return {"score": "n/a"}
        return {"score": 2.0 if G.genotype_hash(genes, genes) == cid else 1.0}

    r = _run(cand, ev, contexts=ctx, mc=2)["results"][0]
    assert r["verdict"] == "insufficient"


def test_parent_and_repeats_are_dropped_and_raw_genes_accepted():
    parent = G.current()
    cand = _cands(2)
    genes = [dict(parent), cand[0]["genotype"]["genes"], cand[0]["genotype"]["genes"],
             cand[1]]
    r = _run(genes, T.dry_run_evaluator())
    assert r["dropped"] == 2 and len(r["results"]) == 2


def test_sealed_pool_context_and_nothing_to_compare():
    r = _run([], T.dry_run_evaluator())
    assert r["results"] == [] and r["evaluated"] == 0


def test_deterministic():
    a = T.to_json(_run(_cands(6, seed=3), T.dry_run_evaluator(1)))
    b = T.to_json(_run(_cands(6, seed=3), T.dry_run_evaluator(1)))
    assert a == b
    assert sum(json.loads(a)["counts"].values()) == 6


def test_leaked_candidate_is_excluded_before_any_evaluator_call():
    cand = _cands(3)
    leaked = cand[0]["genotype"]["id"]
    ex = [{"item": "item-h1", "genotype": leaked, "purpose": "tuning", "at": "t"}]
    seen = []

    def ev(genes, context):
        seen.append(G.genotype_hash(genes, genes))
        return {"score": 0.5}

    r = _run(cand, ev, exposures=ex)
    assert r["rejected_holdout"] == {leaked: "leaked: item-h1"} and r["rejected_count"] == 1
    assert leaked not in seen and leaked not in [x["id"] for x in r["results"]]


def test_unsealed_holdout_excludes_everything():
    r = _run(_cands(3), T.dry_run_evaluator(), sealed=None)
    assert r["results"] == [] and r["evaluated"] == 0
    assert set(r["rejected_holdout"].values()) == {"holdout unsealed"}


def test_parse_context():
    assert T.parse_context("model:a:home") == {"axis": "model", "name": "a", "home": True}
    assert T.parse_context("kind:b") == {"axis": "kind", "name": "b", "home": False}
    with pytest.raises(T.TransferError):
        T.parse_context("kind")
    with pytest.raises(T.TransferError):
        T.parse_context("kind:b:away")


def test_rule_arguments_are_validated():
    ev = T.dry_run_evaluator()
    for kw in ({"other_share": 0}, {"other_share": 1.5}, {"tolerance": -0.1},
               {"min_gain": -1}, {"min_other_contexts": 0}):
        with pytest.raises(T.TransferError):
            _run(_cands(), ev, **kw)
    assert T.verdict_for({"a": 1.0, "b": 1.0}, [{"name": "a", "home": True},
                                                 {"name": "b", "home": False}])[0] == "transfers"


def _cli(*args):
    return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True,
                          encoding="utf-8", timeout=120)


def test_cli_prints_json_writes_only_to_out_and_refuses(tmp_path):
    hold = tmp_path / "hold.json"
    hold.write_text(json.dumps(HOLD), encoding="utf-8")
    seal = tmp_path / "seal.json"
    seal.write_text(json.dumps(H.seal(HOLD)), encoding="utf-8")
    out = tmp_path / "report.json"
    base = ["--max-contexts", "3", "--max-candidates", "4", "--context", "model:a:home",
            "--context", "model:b", "--context", "kind:c"]
    p = _cli(*base, "--holdout", str(hold), "--seal", str(seal), "--out", str(out))
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["budget"] == {"max_contexts": 3, "max_candidates": 4}
    assert json.loads(out.read_text(encoding="utf-8"))["evaluated"] <= (4 + 1) * 3
    assert sorted(os.listdir(tmp_path)) == ["hold.json", "report.json", "seal.json"]
    assert _cli("--max-candidates", "4", "--context", "model:a:home").returncode == 2
    assert _cli("--max-contexts", "0", "--max-candidates", "4",
                "--context", "model:a:home").returncode == 2
    assert _cli("--max-contexts", "1", "--max-candidates", "1").returncode == 2  # no contexts
    assert _cli("--max-contexts", "1", "--max-candidates", "1", "--context", "model:sealed:home"
                ).returncode == 2
    assert _cli("--max-contexts", "1", "--max-candidates", "1", "--context", "model:a:home",
                "--out", os.path.join(str(tmp_path), ".fleet", "r.json")).returncode == 2
