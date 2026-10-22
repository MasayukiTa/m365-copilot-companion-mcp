# -*- coding: utf-8 -*-
"""Read-only: proposer / solver / judge role separation check for candidate records (G3).

WHAT DECISION THIS SERVES. Whether any proposer holds final evaluation authority over its own
candidate, directly or through delegation (rules: relay/org/role_separation.py). It reads one JSON
file, prints the counts, the violations with reasons, and PASS or FAIL. It changes no record, writes
no file, touches no .fleet directory, runs no git, never calls the network or the fleet, has no
clock, and no runtime module imports it.

Usage:
  python scripts/org_role_separation.py --records FILE.json [--format text|json]
FILE.json is a JSON list of candidate records, or an object {"records": [...], "edges": [...]}.
Edges are optional delegation edges, {"from": a, "to": b} or [a, b].

Exit code: 0 PASS, 1 FAIL (at least one violation or malformed record), 2 refused (the file cannot
be read, is not JSON, or has no record list).
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
    from relay.org import role_separation as R
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--records", required=True)
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    try:
        with open(args.records, encoding="utf-8-sig") as fh:
            data = json.loads(fh.read())
    except (OSError, ValueError) as exc:
        sys.stderr.write("refused: %s\n" % exc)
        return 2
    edges = None
    if isinstance(data, dict):
        edges = data.get("edges")
        data = data.get("records")
    if not isinstance(data, list):
        sys.stderr.write("refused: no list of records\n")
        return 2
    report = R.build_report(data, edges)
    if args.format == "json":
        text = R.to_json(report)
    else:
        vc = report["violation_counts"]
        lines = ["role separation: %s" % ("PASS" if report["passed"] else "FAIL"),
                 "  %d records, %d checked, %d malformed, %d flagged, %d clean"
                 % (report["total"], report["checked"], report["malformed"],
                    report["flagged_candidates"], report["clean_candidates"]),
                 "  violations: " + ", ".join("%s=%d" % (c, vc[c]) for c in R.CODES)]
        for v in report["violations"]:
            lines.append("    %s [%s]: %s" % (v["candidate_id"] or "(no id)", v["code"], v["reason"]))
        text = "\n".join(lines)
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
