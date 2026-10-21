# -*- coding: utf-8 -*-
"""Read-only: print the count-cap check and sweep plan for Evolution Lab candidates (slice F2).

WHAT DECISION THIS SERVES. Whether lab candidate branches/worktrees are within their caps and
which ones a human may remove (rules and reasons: relay/org/lab_candidates.py). It reads one JSON
file of records and prints a plan. It deletes nothing, runs no git, writes no file and no runtime
module imports it.

Usage:
  python scripts/org_lab_candidates.py --check --records FILE.json --lab-root LAB_ROOT
         --max-active N --max-total N --max-age-hours H --now ISO8601 [--format text|json]
FILE.json is a list of {"id","branch","worktree","created_at","state"} (or {"candidates": [...]}).
--now is passed in on purpose: the check has no clock, so the same input gives the same plan.

Exit code: 0 ok, 1 violations or over cap, 2 refused (bad input).
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
    from relay.org import lab_candidates as C
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", required=True)
    ap.add_argument("--records", required=True)
    ap.add_argument("--lab-root", required=True)
    ap.add_argument("--max-active", type=int, required=True)
    ap.add_argument("--max-total", type=int, required=True)
    ap.add_argument("--max-age-hours", type=float, required=True)
    ap.add_argument("--now", required=True)
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    try:
        with open(args.records, encoding="utf-8-sig") as fh:
            data = json.loads(fh.read())
        if isinstance(data, dict):
            data = data.get("candidates")
        plan = C.plan_sweep(
            data, {"max_active": args.max_active, "max_total": args.max_total,
                   "max_age_hours": args.max_age_hours}, args.lab_root, args.now)
    except (OSError, ValueError) as exc:
        sys.stderr.write("refused: %s\n" % exc)
        return 2
    if args.format == "json":
        text = C.to_json(plan)
    else:
        lines = ["lab candidates: %s" % ("ok" if plan["ok"] else "NOT OK"),
                 "count %d (active %d), unresolved excess %d"
                 % (plan["count"], plan["active"], plan["unresolved_excess"])]
        for x in plan["over_cap"]:
            lines.append("  over %s: %d > %d" % (x["cap"], x["count"], x["limit"]))
        for x in plan["violations"]:
            lines.append("  violation %s [%s]: %s" % (x["code"], x["id"], x["detail"]))
        for x in plan["sweepable"]:
            lines.append("  sweepable %s (%s): %s" % (x["id"], x["reason"], x["branch"]))
        text = "\n".join(lines)
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    return 0 if plan["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
