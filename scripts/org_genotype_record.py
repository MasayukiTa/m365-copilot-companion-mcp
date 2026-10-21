# -*- coding: utf-8 -*-
"""Operator tool: record candidate organisation genotypes (slice E1-b, record-only).

Appends to `<fleet dir>/org_genotypes.jsonl` (relay/org/genotype_ledger.py). Applies and runs
nothing; no run reads the file. Nothing calls this script except a person.

  python scripts/org_genotype_record.py --fleet-dir DIR list
  python scripts/org_genotype_record.py --fleet-dir DIR add --candidates FILE [--parent ID]

FILE is JSON: one object, or a list, each {"genes": {...}} (the format of
scripts/org_genotype_report.py --candidates). Exit status 0 when every candidate was recorded,
1 when any was refused.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def main(argv=None) -> int:
    from relay.org import genotype_ledger as L
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fleet-dir", required=True, help="state directory that holds the ledger")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a = sub.add_parser("add")
    a.add_argument("--candidates", required=True)
    a.add_argument("--parent", default="")
    args = ap.parse_args(argv)
    path = L.ledger_path(args.fleet_dir)
    if args.cmd == "list":
        entries, bad = L.load(path)
        print(json.dumps({"entries": [{k: e[k] for k in ("id", "archive_id", "parent")
                                       if k in e} for e in entries], "unreadable": bad},
                         ensure_ascii=False, sort_keys=True))
        return 0
    with io.open(args.candidates, encoding="utf-8-sig") as fh:
        data = json.load(fh)
    cands = data if isinstance(data, list) else [data]
    ok = True
    for c in cands:
        genes = c.get("genes") if isinstance(c, dict) else None
        res = L.record(path, genes if isinstance(genes, dict) else {"": None},
                       parent=args.parent)
        ok = ok and res["status"] == "recorded"
        print(json.dumps(res, ensure_ascii=False, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
