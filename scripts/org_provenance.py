# -*- coding: utf-8 -*-
"""Read-only: data provenance and contamination report for run records (G2).

WHAT DECISION THIS SERVES. Whether a sealed holdout is still held out: which items were used for
design or debugging, which were reused across design and holdout, and whose id or text reached the
candidate-generation inputs (rules: relay/org/provenance.py). It reads one JSONL file, prints the
report. It writes no file, touches no .fleet directory, runs no git, never calls the network or the
fleet, has no clock, and no runtime module imports it.

Usage:
  python scripts/org_provenance.py FILE.jsonl [--format text|json]
FILE.jsonl has one JSON run record per line. A line that is not JSON becomes a malformed row.

Exit code: 0 report printed, 2 refused (the file cannot be read).
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


def main(argv=None):
    from relay.org import provenance as P
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    try:
        rows = read_rows(args.path)
    except (OSError, UnicodeDecodeError) as exc:
        sys.stderr.write("refused: %s\n" % exc)
        return 2
    report = P.build_report(rows)
    if args.format == "json":
        text = P.to_json(report)
    else:
        fc = report["flag_counts"]
        lines = ["provenance: %d records, %d malformed, %d items (%d holdout, %d design)"
                 % (report["total"], report["malformed"], report["items"],
                    report["holdout_items"], report["design_items"]),
                 "  flags: " + ", ".join("%s=%d" % (f, fc[f]) for f in P.FLAGS),
                 "  contaminated holdout items: %d, clean holdout items: %d"
                 % (len(report["contaminated_holdout_items"]), len(report["clean_holdout_items"]))]
        for iid in report["contaminated_holdout_items"]:
            lines.append("    %s: %s" % (iid, ", ".join(report["provenance"][iid]["flags"])))
        text = "\n".join(lines)
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
