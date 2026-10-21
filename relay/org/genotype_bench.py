# -*- coding: utf-8 -*-
"""Benchmark runner for genotype candidates (slice E3): run a parent and its candidates through a
fixed evaluator, pool by pool, and report each candidate against the parent.

WHAT DECISION THIS SERVES. Whether a candidate (relay/org/genotype_candidates) did better or
worse than its parent on the pools the optimiser may read, and whether it is allowed to touch the
sealed pool. This module only calls the evaluator it is handed and returns a report; it starts no
job, submits nothing to the fleet, opens no network connection and writes no file (nothing under
.fleet). Nothing in production calls it (default off): only scripts/org_genotype_bench.py, run by
hand, and only to print a plan.

THE EVALUATOR IS THE FROZEN HARNESS. `evaluate(genes, pool)` is passed in; this module does not
choose it, wrap it with retries or tune it between runs, so every genotype in one call is measured
by the same function. It returns a mapping with a numeric "score" (higher is better); anything
else is carried through untouched. An evaluator that raises, or returns no numeric score, is
recorded for that run as an infrastructure failure ("error") and is not scored as a loss, so a
broken harness cannot make a candidate look worse than the parent.

THE SEALED POOL IS SPENT, NOT SHARED. The sealed pool is read only at a milestone (milestone=True)
and each genotype id may be measured on it once: the caller owns a `sealed_used` set, an id found
in it raises SealedReuse before ANY run starts, and ids are added only after their sealed run was
made. Measuring a set repeatedly turns it into optimisation feedback (bench/companionbench/pools.py
states the same reason); the in-memory set is the guard against reuse inside one session, and
keeping it across sessions is the caller's job. Without milestone=True a plan that names the
sealed pool is refused.
"""
from __future__ import annotations

import numbers

from relay.org import genotype as G

EVOLUTION = "evolution"
REGRESSION = "regression"
SEALED = "sealed"
POOLS = (EVOLUTION, REGRESSION, SEALED)
DEFAULT_POOLS = (EVOLUTION, REGRESSION)


class BenchError(ValueError):
    """The request cannot be run as asked (unknown pool, sealed without milestone, bad input)."""


class SealedReuse(BenchError):
    """A genotype id that already used its one sealed run was asked to use it again."""


def _pools(pools, milestone):
    pools = tuple(pools)
    if not pools:
        raise BenchError("no pools requested")
    for p in pools:
        if p not in POOLS:
            raise BenchError("unknown pool %r" % (p,))
    if len(set(pools)) != len(pools):
        raise BenchError("a pool is named twice")
    if SEALED in pools and not milestone:
        raise BenchError("the sealed pool is read at a milestone only (milestone=True)")
    return pools


def _distinct(parent_genes, candidates):
    """(parent_id, [(id, genes)]) with the parent and repeated programs dropped, order kept."""
    parent_genes = dict(parent_genes)
    G.validate(parent_genes)
    pid = G.genotype_hash(parent_genes, parent_genes)
    seen = {pid}
    out = []
    for c in candidates:
        genes = dict(c["genotype"]["genes"])
        G.validate(genes)
        h = G.genotype_hash(genes, genes)
        if h in seen:
            continue
        seen.add(h)
        out.append((h, genes))
    return pid, out


def plan(parent_genes, candidates, pools=DEFAULT_POOLS, milestone=False):
    """The runs a call to run() would make, as [{"genotype", "role", "pool"}] in run order.

    The parent comes first on every pool, so a candidate is always compared with a baseline
    measured by the same evaluator in the same call."""
    pools = _pools(pools, milestone)
    pid, cands = _distinct(parent_genes, candidates)
    runs = []
    for pool in pools:
        runs.append({"genotype": pid, "role": "parent", "pool": pool})
        for h, _ in cands:
            runs.append({"genotype": h, "role": "candidate", "pool": pool})
    return runs


def _score(result):
    s = result.get("score") if isinstance(result, dict) else None
    if isinstance(s, bool) or not isinstance(s, numbers.Real) or s != s:
        return None
    return float(s)


def _measure(evaluate, genes, pool):
    try:
        result = evaluate(dict(genes), pool)
    except Exception as exc:  # noqa: BLE001 - any harness failure is an infra failure, not a loss
        return {"status": "error", "error": "%s: %s" % (type(exc).__name__, exc), "score": None}
    s = _score(result)
    if s is None:
        return {"status": "error", "error": "no numeric score", "score": None}
    return {"status": "ok", "score": s, "result": result}


def run(parent_genes, candidates, evaluate, pools=DEFAULT_POOLS, milestone=False,
        sealed_used=None, harness_id=""):
    """Measure the parent and each distinct candidate on `pools` and report the differences.

    Returns {"harness": harness_id, "pools": [...], "parent": id, "runs": [...],
    "candidates": [{"id", "mutation", "pools": {pool: {"score", "parent_score", "delta",
    "verdict"}}}]}. verdict is "better" / "worse" / "same" by score, or "error" when either side
    errored; no threshold or significance claim is made here. `sealed_used` (a set) is checked
    before any run starts and updated after each sealed run."""
    pools = _pools(pools, milestone)
    pid, cands = _distinct(parent_genes, candidates)
    if SEALED in pools:
        if sealed_used is None:
            raise BenchError("the sealed pool needs the caller's sealed_used set")
        spent = [i for i in [pid] + [h for h, _ in cands] if i in sealed_used]
        if spent:
            raise SealedReuse("already measured on the sealed pool: %s" % ", ".join(spent))
    mutation = {}
    for c in candidates:
        mutation.setdefault(c["genotype"]["id"], c.get("mutation"))
    runs = []
    table = {}
    for pool in pools:
        for h, genes in [(pid, dict(parent_genes))] + cands:
            m = _measure(evaluate, genes, pool)
            if pool == SEALED:
                sealed_used.add(h)
            runs.append({"genotype": h, "pool": pool, "status": m["status"],
                         "score": m["score"], "error": m.get("error", "")})
            table[(h, pool)] = m["score"]
    out = []
    for h, _ in cands:
        per = {}
        for pool in pools:
            s, ps = table[(h, pool)], table[(pid, pool)]
            if s is None or ps is None:
                per[pool] = {"score": s, "parent_score": ps, "delta": None, "verdict": "error"}
            else:
                d = s - ps
                per[pool] = {"score": s, "parent_score": ps, "delta": d,
                             "verdict": "better" if d > 0 else "worse" if d < 0 else "same"}
        out.append({"id": h, "mutation": mutation.get(h), "pools": per})
    return {"harness": str(harness_id), "pools": list(pools), "parent": pid, "runs": runs,
            "candidates": out}
