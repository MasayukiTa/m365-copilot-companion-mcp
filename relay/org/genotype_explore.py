# -*- coding: utf-8 -*-
"""Bounded exploration loop for the organisation genotype (slice E5): E2 -> E3 -> E4 -> E7.

WHAT DECISION THIS SERVES. Whether any single-gene neighbour of a parent genotype is worth keeping
in the Pareto archive, found by one bounded, reproducible search instead of hand-picked runs. Per
round it generates candidates from the current parent (genotype_candidates), measures them with an
evaluator that is passed in (genotype_bench), refuses any candidate whose holdout exposure makes
its score untrustworthy (genotype_holdout), and inserts the rest into the multi-objective archive
(genotype_archive). It starts no job, submits nothing to the fleet, opens no network connection
and writes no file (nothing under .fleet). Nothing in production calls it (default off): only
scripts/org_genotype_explore.py, run by hand, does, and that script writes only to the --out path
it is given.

HARD BUDGET. max_rounds and max_candidates are required and must be integers >= 1; there is no
default and no way to run unbounded. max_rounds caps the number of generate/measure rounds.
max_candidates caps the number of distinct candidates generated over ALL rounds. The purpose of
both is to bound evaluator cost before the loop starts: each round measures the parent and its
candidates once per pool, so the evaluator is called at most
max_rounds * len(pools) + max_candidates * len(pools) times (the evaluator is the expensive,
fleet-backed part in real use). The loop also stops early, and says why, when a round yields no
candidate that beats its parent on the success axis (stop_reason "no_improvement"), or when the
neighbourhood holds nothing unseen ("exhausted").

DETERMINISM. With the same parent, seed, budgets, holdout inputs and a deterministic evaluator the
report is identical: round r draws candidates with seed + r, candidates are never repeated across
rounds (a program already seen is dropped), and ties for "best" go to the smaller genotype id.

HOLDOUT CHECK BEFORE ACCEPTANCE. After a round is measured and before anything enters the archive,
genotype_holdout.verdict() is computed from the seal, the dev ids, the holdout ids and the
exposure list. If the verdict is unsealed, or has a digest mismatch or a dev/holdout overlap, the
holdout is not trustworthy for anybody and every candidate of the round is rejected. Otherwise a
candidate is rejected when it is named in a leaked or over_budget finding. Rejected candidates are
counted in the report ("rejected_holdout", with the reason per id) and never reach the archive or
become the next parent. The parent's own baseline entry is inserted once, in round 1, unless the
parent itself is rejected on the same grounds.
"""
from __future__ import annotations

import hashlib
import json

from relay.org import genotype as G
from relay.org import genotype_archive as A
from relay.org import genotype_bench as B
from relay.org import genotype_candidates as C
from relay.org import genotype_holdout as H

VERSION = 1


class ExploreError(ValueError):
    """The request cannot be run as asked (missing or non-positive budget, bad input)."""


def _budget(name, value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ExploreError("%s must be an integer >= 1" % name)
    return value


def dry_run_evaluator(salt=0):
    """A deterministic stand-in evaluator for dry runs: score in [0, 1) from the genes alone.

    It measures nothing real; it exists so the loop can be exercised end to end without a fleet.
    Its reports are synthetic and must not be read as benchmark results."""
    def evaluate(genes, pool):
        blob = json.dumps([salt, pool, genes], sort_keys=True, default=str).encode("utf-8")
        n = int(hashlib.sha256(blob).hexdigest()[:8], 16)
        return {"score": n / float(16 ** 8), "synthetic": True}
    return evaluate


def holdout_rejections(ids, sealed, dev_ids, holdout_ids, exposures, budget=H.DEFAULT_BUDGET):
    """{id: reason} for the genotype ids in `ids` whose scores cannot be trusted, plus the verdict.

    Returns (rejected, verdict). Everything is rejected when the holdout itself is unusable
    (unsealed, digest mismatch, overlap with dev); otherwise only ids named in a leaked or
    over_budget finding."""
    v = H.verdict(sealed, dev_ids, holdout_ids, exposures, budget=budget)
    f = v["findings"]
    if v["state"] == "unsealed":
        return {i: "holdout unsealed" for i in ids}, v
    if f["digest_mismatch"] or f["overlap"]:
        why = "holdout digest mismatch" if f["digest_mismatch"] else "holdout overlaps dev"
        return {i: why for i in ids}, v
    named = {}
    for x in f["leaked"]:
        for g in x["genotypes"]:
            named.setdefault(g, "leaked: %s" % x["item"])
    for x in f["over_budget"]:
        for g in x["genotypes"]:
            named.setdefault(g, "over budget: %s" % x["item"])
    return {i: named[i] for i in ids if i in named}, v


def _success(entry):
    return (entry.get("axes") or {}).get("success")


def explore(parent_genes, evaluate, max_rounds, max_candidates, seed=0, pools=B.DEFAULT_POOLS,
            sealed=None, dev_ids=None, holdout_ids=None, exposures=None,
            budget=H.DEFAULT_BUDGET, harness_id=""):
    """Run the bounded loop and return a report dict (nothing is written anywhere).

    Report: {"version", "harness", "seed", "budget": {"max_rounds", "max_candidates"},
    "parent": id, "rounds": [{"round", "parent", "generated", "evaluated", "rejected_holdout",
    "inserted", "best"}], "generated", "evaluated", "rejected_holdout": {id: reason},
    "rejected_count", "stop_reason", "archive"}. Only the evolution/regression style pools of
    genotype_bench are allowed here: the sealed pool is a milestone matter and is refused."""
    max_rounds = _budget("max_rounds", max_rounds)
    max_candidates = _budget("max_candidates", max_candidates)
    pools = tuple(pools)
    if B.SEALED in pools:
        raise ExploreError("the exploration loop never reads the sealed pool")
    parent_genes = dict(parent_genes)
    G.validate(parent_genes)
    root = G.genotype_hash(parent_genes, parent_genes)
    seen = {root}
    cur_id, cur_genes = root, parent_genes
    archive = A.empty()
    rounds, rejected_all = [], {}
    generated = evaluated = 0
    stop = "max_rounds"
    for r in range(1, max_rounds + 1):
        left = max_candidates - generated
        if left <= 0:
            stop = "max_candidates"
            break
        batch = C.generate(cur_genes, left + len(seen), seed=seed + r)
        batch = [c for c in batch if c["genotype"]["id"] not in seen][:left]
        if not batch:
            stop = "exhausted"
            break
        for c in batch:
            seen.add(c["genotype"]["id"])
        generated += len(batch)
        report = B.run(cur_genes, batch, evaluate, pools=pools, harness_id=harness_id)
        evaluated += len(report["runs"])
        ids = [c["id"] for c in report["candidates"]]
        check_ids = ids + ([cur_id] if r == 1 else [])
        rejected, _ = holdout_rejections(check_ids, sealed, dev_ids, holdout_ids,
                                         exposures, budget=budget)
        rejected_all.update(rejected)
        kept = [e for e in A.entries_from_report(report) if e["id"] not in rejected]
        for e in kept:
            if e["id"] == cur_id and r > 1:
                continue  # the parent of a later round is already archived
            archive = A.insert(archive, e)
        by_id = {e["id"]: e for e in kept}
        parent_score = _success(by_id[cur_id]) if cur_id in by_id else None
        better = sorted((i for i in ids if i in by_id and _success(by_id[i]) is not None
                         and (parent_score is None or _success(by_id[i]) > parent_score)),
                        key=lambda i: (-_success(by_id[i]), i))
        rounds.append({"round": r, "parent": cur_id, "generated": ids,
                       "evaluated": len(report["runs"]),
                       "rejected_holdout": sorted(i for i in ids if i in rejected),
                       "inserted": sorted(i for i in ids if i in by_id),
                       "best": better[0] if better else None})
        if not better:
            stop = "no_improvement"
            break
        cur_id = better[0]
        cur_genes = next(dict(c["genotype"]["genes"]) for c in batch
                         if c["genotype"]["id"] == cur_id)
    return {"version": VERSION, "harness": str(harness_id), "seed": seed,
            "budget": {"max_rounds": max_rounds, "max_candidates": max_candidates},
            "parent": root, "rounds": rounds, "generated": generated, "evaluated": evaluated,
            "rejected_holdout": rejected_all, "rejected_count": len(rejected_all),
            "stop_reason": stop, "archive": archive}


def to_json(report):
    return json.dumps(report, sort_keys=True, indent=2)
