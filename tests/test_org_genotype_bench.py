# -*- coding: utf-8 -*-
"""Benchmark runner for genotype candidates (slice E3): fixed evaluator, sealed pool spent once."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.org import genotype as G  # noqa: E402
from relay.org import genotype_bench as B  # noqa: E402
from relay.org import genotype_candidates as C  # noqa: E402

SCRIPT = os.path.join(REPO, "scripts", "org_genotype_bench.py")


def _setup(n=4):
    parent = G.current()
    return parent, C.generate(parent, n, seed=1)


def _by_hash(parent, cands, bonus):
    """Evaluator scoring a genotype by a fixed table keyed on its program hash."""
    table = {G.genotype_hash(parent, parent): 1.0}
    for i, c in enumerate(cands):
        table[c["genotype"]["id"]] = bonus[i]
    return lambda genes, pool: {"score": table[G.genotype_hash(genes, genes)]}


def test_plan_puts_parent_first_per_pool_and_dedups():
    parent, cands = _setup()
    runs = B.plan(parent, cands + cands)
    assert len(runs) == 2 * (1 + len(cands))
    assert runs[0]["role"] == "parent" and runs[0]["pool"] == B.EVOLUTION
    assert [r["role"] for r in runs if r["pool"] == B.REGRESSION][0] == "parent"


def test_sealed_needs_milestone_and_unknown_pool_refused():
    parent, cands = _setup()
    with pytest.raises(B.BenchError):
        B.plan(parent, cands, pools=("sealed",))
    with pytest.raises(B.BenchError):
        B.plan(parent, cands, pools=("nope",))
    with pytest.raises(B.BenchError):
        B.plan(parent, cands, pools=())
    assert B.plan(parent, cands, pools=("sealed",), milestone=True)


def test_report_compares_each_candidate_with_the_parent():
    parent, cands = _setup(3)
    rep = B.run(parent, cands, _by_hash(parent, cands, [2.0, 1.0, 0.5]), harness_id="h")
    verdicts = [c["pools"][B.EVOLUTION]["verdict"] for c in rep["candidates"]]
    assert verdicts == ["better", "same", "worse"]
    assert rep["candidates"][0]["pools"][B.EVOLUTION]["delta"] == 1.0
    assert rep["harness"] == "h" and rep["pools"] == [B.EVOLUTION, B.REGRESSION]
    assert rep["candidates"][0]["mutation"] == cands[0]["mutation"]


def test_a_failing_evaluator_is_an_error_not_a_loss():
    parent, cands = _setup(2)
    bad = cands[0]["genotype"]["id"]

    def ev(genes, pool):
        if G.genotype_hash(genes, genes) == bad:
            raise RuntimeError("harness down")
        if G.genotype_hash(genes, genes) == cands[1]["genotype"]["id"]:
            return {"score": float("nan")}
        return {"score": 1.0}

    rep = B.run(parent, cands, ev)
    assert [c["pools"][B.EVOLUTION]["verdict"] for c in rep["candidates"]] == ["error", "error"]
    assert any(r["status"] == "error" and "harness down" in r["error"] for r in rep["runs"])


def test_sealed_pool_is_spent_once_and_checked_before_any_run():
    parent, cands = _setup(2)
    used = set()
    calls = []

    def ev(genes, pool):
        calls.append(pool)
        return {"score": 1.0}

    with pytest.raises(B.BenchError):
        B.run(parent, cands, ev, pools=("sealed",), milestone=True)
    assert calls == []
    B.run(parent, cands, ev, pools=("sealed",), milestone=True, sealed_used=used)
    assert len(used) == 1 + len(cands) and len(calls) == 1 + len(cands)
    calls.clear()
    with pytest.raises(B.SealedReuse):
        B.run(parent, cands, ev, pools=("evolution", "sealed"), milestone=True, sealed_used=used)
    assert calls == []


def test_invalid_parent_is_refused():
    parent, cands = _setup(1)
    bad = dict(parent)
    bad["effort"] = "not-a-level"
    with pytest.raises(G.GenotypeError):
        B.run(bad, cands, lambda g, p: {"score": 1.0})


def test_cli_prints_a_plan_and_writes_nothing(tmp_path):
    fleet = os.path.join(REPO, ".fleet")
    before = set(os.listdir(fleet)) if os.path.isdir(fleet) else None
    p = subprocess.run([sys.executable, "-I", "-X", "utf8", SCRIPT, "-n", "3"],
                       capture_output=True, check=True, cwd=str(tmp_path))
    assert p.stdout.decode("utf-8").startswith("8 runs for 3 candidates")
    refused = subprocess.run([sys.executable, "-I", "-X", "utf8", SCRIPT, "--pools", "sealed"],
                             capture_output=True, cwd=str(tmp_path))
    assert refused.returncode == 2
    after = set(os.listdir(fleet)) if os.path.isdir(fleet) else None
    assert before == after and os.listdir(str(tmp_path)) == []
