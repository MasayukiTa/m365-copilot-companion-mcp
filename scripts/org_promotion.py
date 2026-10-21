# -*- coding: utf-8 -*-
"""Operator tool: record a promotion or a rollback of an organisation version (slice D6, record-only).

It appends to `<fleet dir>/org_promotions.jsonl` (relay/org/promotion.py) and applies nothing:
no prompt, role or workflow is changed, and no run reads the file. Nothing calls this script
except a person.

  python scripts/org_promotion.py --fleet-dir DIR status
  python scripts/org_promotion.py --fleet-dir DIR promote [--evidence TEXT] [--note TEXT]
  python scripts/org_promotion.py --fleet-dir DIR rollback --reason TEXT [--to GENOTYPE_ID]

`promote` records the version in the source right now. `status` also shows whether the source
still matches the ledger's current version (drift). Exit status 0 on success, 1 when refused.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def main(argv=None) -> int:
    from relay.org import definitions, promotion
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fleet-dir", required=True, help="state directory that holds the ledger")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p = sub.add_parser("promote")
    p.add_argument("--evidence", default="")
    p.add_argument("--note", default="")
    r = sub.add_parser("rollback")
    r.add_argument("--reason", required=True)
    r.add_argument("--to", default="")
    args = ap.parse_args(argv)
    path = promotion.ledger_path(args.fleet_dir)
    if args.cmd == "status":
        out = dict(promotion.status(path))
        out["drift"] = promotion.drift(path)
        res = out
        ok = True
    elif args.cmd == "promote":
        res = promotion.promote(path, definitions.snapshot(), args.evidence, args.note)
        ok = res["status"] == "recorded"
    else:
        res = promotion.rollback(path, args.reason, to=args.to or None)
        ok = res["status"] == "recorded"
    print(json.dumps(res, ensure_ascii=False, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
