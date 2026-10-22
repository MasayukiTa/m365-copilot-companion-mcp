# -*- coding: utf-8 -*-
"""Read-only: shadow verdict for an Evolution Lab candidate against the production baseline (F6).

WHAT DECISION THIS SERVES. Whether a candidate measured in shadow is not worse than the production
baseline beyond a tolerance, on enough common tasks, with a passing security regression report
(rules, constants and reasons: relay/org/lab_canary.py). It reads three JSON files and prints the
verdict. It writes no file, runs no git, runs no benchmark, never calls the fleet, never touches
production state, has no clock, and no runtime module imports it.

Usage:
  python scripts/org_lab_canary.py --candidate RUN.json --baseline RUN.json
         --security REPORT.json [--tolerance 0.02] [--min-samples 30] [--format text|json]
RUN.json is {"bundle_hash": ..., "scores": {task: {metric: number}}}; REPORT.json is the json output
of scripts/org_lab_security_regression.py.

Exit code: 0 pass, 1 fail or insufficient, 2 refused (bad input).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def _load(path):
    with open(path, encoding="utf-8-sig") as fh:
        return json.loads(fh.read())


def main(argv=None):
    from relay.org import lab_canary as C
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--security", required=True)
    ap.add_argument("--tolerance", type=float, default=C.DEFAULT_TOLERANCE)
    ap.add_argument("--min-samples", type=int, default=C.DEFAULT_MIN_SAMPLES)
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    try:
        report = C.shadow_verdict(_load(args.candidate), _load(args.baseline), _load(args.security),
                                  tolerance=args.tolerance, min_samples=args.min_samples)
    except (OSError, ValueError) as exc:
        sys.stderr.write("refused: %s\n" % exc)
        return 2
    if args.format == "json":
        text = C.to_json(report)
    else:
        lines = ["lab canary: %s (%d common task(s), tolerance %s, minimum %d)"
                 % (report["verdict"].upper(), report["samples"], report["tolerance"],
                    report["min_samples"])]
        for name in sorted(report["metrics"]):
            m = report["metrics"][name]
            lines.append("  %-20s candidate %.4f baseline %.4f delta %+.4f%s"
                         % (name, m["candidate"], m["baseline"], m["delta"],
                            "  REGRESSED" if m["regressed"] else ""))
        for p in report["problems"]:
            lines.append("  problem %s [%s]: %s" % (p["code"], p["id"], p["detail"]))
        if report["only_candidate"]:
            lines.append("  only in candidate: %s" % ", ".join(report["only_candidate"]))
        if report["only_baseline"]:
            lines.append("  only in baseline: %s" % ", ".join(report["only_baseline"]))
        text = "\n".join(lines)
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
