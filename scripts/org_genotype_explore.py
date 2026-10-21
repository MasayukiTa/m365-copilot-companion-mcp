# -*- coding: utf-8 -*-
"""Read-only: run the bounded genotype exploration loop and print its report (slice E5).

WHAT DECISION THIS SERVES. Whether any neighbour of a parent genotype earns a place in the Pareto
archive within a fixed budget (see relay/org/genotype_explore.py). It submits no job, touches
nothing under .fleet, and no runtime module imports it. The only file it ever writes is the one
named by --out.

Usage:  python scripts/org_genotype_explore.py --rounds 3 --max-candidates 12 [--seed 0]
            [--parent-json FILE] [--evaluator MODULE:FUNCTION] [--pools evolution,regression]
            [--holdout H.json --seal SEAL.json] [--dev DEV.json] [--exposures EX.json]
            [--holdout-budget N] [--out REPORT.json]

Without --evaluator a deterministic synthetic evaluator is used (a dry run: the scores mean
nothing). MODULE:FUNCTION must be a callable evaluate(genes, pool) -> {"score": number}.
HOLDOUT.json / DEV.json are lists of ids, EXPOSURES.json a list of {"item", "genotype",
"purpose", "at"}. Without --seal every candidate is rejected as "holdout unsealed".
Exit code: 0 done, 2 refused (bad input).
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def _load(path):
    with open(path, encoding="utf-8-sig") as fh:
        return json.loads(fh.read())


def _evaluator(spec, E):
    if not spec:
        return E.dry_run_evaluator()
    mod, _, name = spec.partition(":")
    if not mod or not name:
        raise E.ExploreError("--evaluator needs MODULE:FUNCTION")
    return getattr(importlib.import_module(mod), name)


def main(argv=None):
    from relay.org import genotype as G
    from relay.org import genotype_bench as B
    from relay.org import genotype_explore as E
    from relay.org import genotype_holdout as H
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rounds", type=int, required=True)
    ap.add_argument("--max-candidates", type=int, required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--parent-json", default="")
    ap.add_argument("--evaluator", default="")
    ap.add_argument("--pools", default=",".join(B.DEFAULT_POOLS))
    ap.add_argument("--holdout")
    ap.add_argument("--seal")
    ap.add_argument("--dev")
    ap.add_argument("--exposures")
    ap.add_argument("--holdout-budget", type=int, default=H.DEFAULT_BUDGET)
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)
    try:
        if args.out and ".fleet" in os.path.normpath(os.path.abspath(args.out)).split(os.sep):
            raise E.ExploreError("--out must not be under .fleet")
        if args.parent_json:
            data = _load(args.parent_json)
            parent = data.get("genes", data) if isinstance(data, dict) else data
        else:
            parent = G.current()
        report = E.explore(
            parent, _evaluator(args.evaluator, E), args.rounds, args.max_candidates,
            seed=args.seed, pools=[p for p in args.pools.split(",") if p],
            sealed=_load(args.seal) if args.seal else None,
            dev_ids=_load(args.dev) if args.dev else [],
            holdout_ids=_load(args.holdout) if args.holdout else [],
            exposures=_load(args.exposures) if args.exposures else [],
            budget=args.holdout_budget, harness_id=args.evaluator or "dry-run")
    except (OSError, ImportError, AttributeError, ValueError) as exc:
        sys.stderr.write("refused: %s\n" % exc)
        return 2
    text = E.to_json(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
    lines = ["rounds run: %d of %d  stop: %s" % (len(report["rounds"]), args.rounds,
                                                  report["stop_reason"]),
             "candidates generated: %d  evaluator runs: %d" % (report["generated"],
                                                               report["evaluated"]),
             "rejected by holdout check: %d" % report["rejected_count"],
             "archive front: %d" % len(report["archive"]["entries"])]
    sys.stdout.buffer.write(("\n".join(lines) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
