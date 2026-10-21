# -*- coding: utf-8 -*-
"""Read-only: print the strict lineage check for Evolution Lab experiments (slice F3).

WHAT DECISION THIS SERVES. Whether lab candidates form a trustworthy family tree before anything
is promoted or archived (rules and reasons: relay/org/lab_lineage.py). It reads one JSON file of
records, prints the findings and a tree summary. It writes no file, runs no git, has no clock and
no runtime module imports it.

Usage:
  python scripts/org_lab_lineage.py --check --records FILE.json [--format text|json]
FILE.json is a list of {"experiment_id","candidate_id","parent_ids","genotype_hash",
"created_at","state"} (or {"experiments": [...]}).

Exit code: 0 ok, 1 violations, 2 refused (bad input).
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
    from relay.org import lab_lineage as L
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", required=True)
    ap.add_argument("--records", required=True)
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    try:
        with open(args.records, encoding="utf-8-sig") as fh:
            data = json.loads(fh.read())
        if isinstance(data, dict):
            data = data.get("experiments")
        report = L.check_lineage(data)
        report["summary"] = L.summarize(data)
    except (OSError, ValueError) as exc:
        sys.stderr.write("refused: %s\n" % exc)
        return 2
    if args.format == "json":
        text = L.to_json(report)
    else:
        s = report["summary"]
        lines = ["lab lineage: %s" % ("ok" if report["ok"] else "NOT OK"),
                 "count %d, depth %d, roots %d, max branching %d, mean branching %s"
                 % (s["count"], s["depth"], s["roots"], s["max_branching"], s["mean_branching"])]
        for x in report["violations"]:
            lines.append("  violation %s [%s]: %s" % (x["code"], x["id"], x["detail"]))
        text = "\n".join(lines)
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
