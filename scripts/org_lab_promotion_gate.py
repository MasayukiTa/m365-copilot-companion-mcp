# -*- coding: utf-8 -*-
"""Read-only: promotion gate verdict for an Evolution Lab candidate (F7).

WHAT DECISION THIS SERVES. Whether a lab candidate may be promoted, from the combined evidence of
the isolation, lineage, bench bundle, security regression and canary slices (rules and reasons:
relay/org/lab_promotion_gate.py). It reads one JSON evidence file and prints allow or deny with the
reasons. It promotes nothing, writes no file, edits no frozen file, runs no git, never calls the
fleet, has no clock, and no runtime module imports it.

Usage:
  python scripts/org_lab_promotion_gate.py --evidence FILE.json [--format text|json]
FILE.json is {"candidate_id", "isolation", "lineage", "bundle", "security", "canary"}; see the
module docstring for what each key holds. A missing key is a deny, not an error.

Exit code: 0 allow, 1 deny, 2 refused (the file cannot be read or is not JSON).
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
    from relay.org import lab_promotion_gate as G
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
    report = G.gate_verdict(evidence)
    if args.format == "json":
        text = G.to_json(report)
    else:
        lines = ["lab promotion gate: %s (candidate %s)" % (report["verdict"].upper(),
                                                            report["candidate_id"])]
        for r in report["reasons"]:
            lines.append("  reason %s [%s]: %s" % (r["code"], r["input"], r["detail"]))
        text = "\n".join(lines)
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    return 0 if report["allow"] else 1


if __name__ == "__main__":
    sys.exit(main())
