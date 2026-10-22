# -*- coding: utf-8 -*-
"""Read-only: per-agent reputation report from evaluation records (G5).

WHAT DECISION THIS SERVES. How far an agent's reputation can be trusted: enough independent
evaluators, no self-evaluation, no flood from one evaluator, no mutual back-scratching (rules:
relay/org/reputation.py). It reads one JSON file, prints the reputation per agent with confidence,
the flags with reasons, and PASS or FAIL. It changes no record, writes no file, touches no .fleet
directory, runs no git, never calls the network or the fleet, has no clock, and no runtime module
imports it.

Usage:
  python scripts/org_reputation.py --records FILE.json [--format text|json]
FILE.json is a JSON list of evaluation records, or an object {"records": [...]}. A record is
{"evaluator_id", "subject_id", "score" (0..1), optional "task_id", optional "timestamp" (int)}.

Exit code: 0 PASS, 1 FAIL (a flag other than evaluator_capped, or a malformed record), 2 refused
(the file cannot be read, is not JSON, or has no record list).
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
    from relay.org import reputation as R
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
    if isinstance(data, dict):
        data = data.get("records")
    if not isinstance(data, list):
        sys.stderr.write("refused: no list of records\n")
        return 2
    report = R.build_report(data)
    if args.format == "json":
        text = R.to_json(report)
    else:
        fc = report["flag_counts"]
        lines = ["reputation: %s" % ("PASS" if report["passed"] else "FAIL"),
                 "  %d records, %d checked, %d malformed, %d counted, %d down-weighted"
                 % (report["total"], report["checked"], report["malformed"], report["counted"],
                    report["downweighted_evaluations"]),
                 "  flags: " + ", ".join("%s=%d" % (c, fc[c]) for c in R.CODES)]
        for a in report["agents"]:
            lines.append("    %s: reputation %.3f, confidence %s, %d evaluators"
                         % (a["agent_id"], a["reputation"], a["confidence"],
                            a["contributing_evaluators"]))
        for f in report["flags"]:
            lines.append("    [%s] %s" % (f["code"], f["reason"]))
        for s in report["skipped"]:
            lines.append("    [malformed] record %d: %s" % (s["index"], s["reason"]))
        text = "\n".join(lines)
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
