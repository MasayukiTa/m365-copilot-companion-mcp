# -*- coding: utf-8 -*-
"""Read-only: population novelty and quality-diversity report for org-agent records (G4).

WHAT DECISION THIS SERVES. Whether a population is still diverse: how novel each member is, which
members are copies of each other, and the best quality per niche (rules: relay/org/novelty.py). It
reads one JSONL file and prints the report. It writes no file unless --out is given (then only that
path), touches no .fleet directory, runs no git, never calls the network or the fleet, has no
clock, and no runtime module imports it.

Usage:
  python scripts/org_novelty.py FILE.jsonl [--format text|json] [-k N] [--near D]
                                [--niche NAME ...] [--out PATH]
FILE.jsonl has one JSON member record per line. A line that is not JSON becomes a malformed row.

Exit code: 0 report printed, 2 refused (the file cannot be read, bad option, or --out unwritable).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def read_rows(path):
    """Parse the JSONL file. Blank lines are skipped; a bad line becomes None (malformed)."""
    rows = []
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                rows.append(None)
    return rows


def _fmt(v):
    return "n/a" if v is None else "%.3f" % v


def main(argv=None):
    from relay.org import novelty as N
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    ap.add_argument("-k", type=int, default=N.DEFAULT_K, help="neighbours used for novelty")
    ap.add_argument("--near", type=float, default=0.0,
                    help="structural distance at or below which members are duplicates")
    ap.add_argument("--niche", action="append", default=[], help="a declared niche (repeatable)")
    ap.add_argument("--out", default="", help="also write the report text to this path")
    args = ap.parse_args(argv)
    if args.k < 1 or not (0.0 <= args.near <= 1.0):
        sys.stderr.write("refused: -k must be >= 1 and --near within 0..1\n")
        return 2
    try:
        rows = read_rows(args.path)
    except (OSError, UnicodeDecodeError) as exc:
        sys.stderr.write("refused: %s\n" % exc)
        return 2
    report = N.build_report(rows, k=args.k, near=args.near, niches=args.niche)
    if args.format == "json":
        text = N.to_json(report)
    else:
        qd = report["quality_diversity"]
        cov = "n/a" if qd["coverage"] is None else "%.3f" % qd["coverage"]
        lines = ["novelty: %d records, %d malformed, %d members"
                 % (report["total"], report["malformed"], report["members"]),
                 "  duplicate clusters: %d (%d members)"
                 % (len(report["duplicate_clusters"]), report["members_in_duplicate_clusters"]),
                 "  niches covered: %d, coverage: %s, qd_score: %s"
                 % (len(qd["covered_niches"]), cov, _fmt(qd["qd_score"]))]
        for c in report["duplicate_clusters"]:
            lines.append("    cluster: %s" % ", ".join(c["ids"]))
        for mid, v in report["novelty"].items():
            lines.append("    %s: structural=%s behavioral=%s" % (
                mid, _fmt(v["structural_novelty"]), _fmt(v["behavioral_novelty"])))
        text = "\n".join(lines)
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text + "\n")
        except OSError as exc:
            sys.stderr.write("refused: %s\n" % exc)
            return 2
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
