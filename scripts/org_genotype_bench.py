# -*- coding: utf-8 -*-
"""Read-only: print the benchmark plan for a parent genotype and its candidates (slice E3).

WHAT DECISION THIS SERVES. How many evaluator runs a candidate batch would cost, and on which
pools, before anyone supplies an evaluator. It runs nothing: no evaluator is attached, no job is
submitted, nothing is written (no file under .fleet) and no runtime module imports it.

Usage:  python scripts/org_genotype_bench.py [-n 8] [--seed 0] [--parent-json FILE]
                                             [--pools evolution,regression] [--milestone]
--pools may name sealed only together with --milestone.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def main(argv=None):
    from relay.org import genotype as G
    from relay.org import genotype_bench as B
    from relay.org import genotype_candidates as C
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-n", type=int, default=8)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--parent-json", default="")
    ap.add_argument("--pools", default=",".join(B.DEFAULT_POOLS))
    ap.add_argument("--milestone", action="store_true")
    args = ap.parse_args(argv)
    if args.parent_json:
        with open(args.parent_json, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        parent = data.get("genes", data) if isinstance(data, dict) else data
    else:
        parent = G.current()
    cands = C.generate(parent, args.n, seed=args.seed)
    try:
        runs = B.plan(parent, cands, pools=[p for p in args.pools.split(",") if p],
                      milestone=args.milestone)
    except B.BenchError as exc:
        sys.stderr.write("refused: %s\n" % exc)
        return 2
    lines = ["%d runs for %d candidates" % (len(runs), len(cands))]
    for r in runs:
        lines.append("  %s  %s  %s" % (r["pool"], r["role"], r["genotype"]))
    sys.stdout.buffer.write(("\n".join(lines) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
