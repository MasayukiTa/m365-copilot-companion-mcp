# -*- coding: utf-8 -*-
"""Read-only: print candidate organisation genotypes derived from a parent (slice E2).

WHAT DECISION THIS SERVES. Which single-gene changes to the current (or a given) genotype are
worth a run. It reads the live gene values and prints candidates; it writes nothing (no file under
.fleet, no ledger row) and no runtime module imports it.

Usage:  python scripts/org_genotype_candidates.py [-n 8] [--seed 0] [--parent-json FILE]
                                                  [--format json]
--parent-json names a file holding {"genes": {...}} or a bare gene object; without it the live
values are the parent.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def _text(cands):
    lines = ["%d candidates" % len(cands)]
    for c in cands:
        lines.append("  %s  parent=%s  %s" % (c["genotype"]["id"], c["parent"], c["description"]))
    return "\n".join(lines)


def main(argv=None):
    from relay.org import genotype as G
    from relay.org import genotype_candidates as C
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-n", type=int, default=8)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--parent-json", default="")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    if args.parent_json:
        with open(args.parent_json, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        parent = data.get("genes", data) if isinstance(data, dict) else data
    else:
        parent = G.current()
    cands = C.generate(parent, args.n, seed=args.seed)
    text = json.dumps(cands, ensure_ascii=False, indent=2) if args.format == "json" else _text(cands)
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
