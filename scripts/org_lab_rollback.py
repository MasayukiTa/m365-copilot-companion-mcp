# -*- coding: utf-8 -*-
"""Read-only: rollback plan for a promoted Evolution Lab candidate (F8).

WHAT DECISION THIS SERVES. Whether a candidate that was already promoted should be rolled back,
from the promotion record and the shadow results measured after it (rules and reasons:
relay/org/lab_rollback.py). It reads one JSON evidence file and prints keep, rollback or undecided
with the reasons and, for rollback, the roll_back call that undoes it. It performs no rollback,
promotes nothing, writes no file, edits no frozen file, runs no git, never calls the fleet, has no
clock, and no runtime module imports it.

Usage:
  python scripts/org_lab_rollback.py --evidence FILE.json [--format text|json]
FILE.json is {"candidate_id", "promotion", "observations", "security"}; see the module docstring for
what each key holds. A missing key is "undecided", not an error.

Exit code: 0 keep, 1 rollback, 3 undecided, 2 refused (the file cannot be read or is not JSON).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

EXIT = {"keep": 0, "rollback": 1, "undecided": 3}


def main(argv=None):
    from relay.org import lab_rollback as R
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    try:
        with open(args.evidence, encoding="utf-8-sig") as fh:
            evidence = json.loads(fh.read())
    except (OSError, ValueError) as exc:
        sys.stderr.write("refused: %s\n" % exc)
        return 2
    report = R.rollback_plan(evidence)
    if args.format == "json":
        text = R.to_json(report)
    else:
        lines = ["lab rollback plan: %s (candidate %s)" % (report["decision"].upper(),
                                                          report["candidate_id"])]
        for r in report["reasons"]:
            lines.append("  reason %s [%s]: %s" % (r["code"], r["input"], r["detail"]))
        plan = report["plan"]
        if plan is not None:
            for s in plan["steps"]:
                lines.append("  step: call %s with the recorded restore point (kind %s)"
                             % (s["tool"], s["argument"].get("kind")))
            for n in plan["notes"]:
                lines.append("  note: %s" % n)
        text = "\n".join(lines)
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    return EXIT[report["decision"]]


if __name__ == "__main__":
    sys.exit(main())
