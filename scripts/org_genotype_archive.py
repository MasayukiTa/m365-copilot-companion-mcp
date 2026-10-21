# -*- coding: utf-8 -*-
"""Read-only: print the Pareto front of a benchmark results file (slice E4).

WHAT DECISION THIS SERVES. Which measured genotypes are not beaten on every axis by another one
(success, wall time, turns, tool calls, retries, infra failures, regressions, human
interventions). It reads one JSON file (the report relay/org/genotype_bench.run() returns) and
prints; it writes nothing (no file under .fleet), submits no job and no runtime module imports it.

Usage:  python scripts/org_genotype_archive.py RESULTS.json [--format text|json]
"""
from __future__ import annotations

import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def main(argv=None):
    import json

    from relay.org import genotype_archive as A
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("results")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    try:
        with open(args.results, encoding="utf-8-sig") as fh:
            report = json.load(fh)
        archive = A.build(A.entries_from_report(report))
    except (OSError, ValueError) as exc:  # ArchiveError is a ValueError
        sys.stderr.write("refused: %s\n" % exc)
        return 2
    if args.format == "json":
        text = A.to_json(archive)
    else:
        lines = ["%d entries on the front" % len(archive["entries"])]
        for e in archive["entries"]:
            cols = ["%s=%s" % (n, e["axes"][n]) if n in e["axes"] else "%s=-" % n
                    for n in A.AXIS_NAMES]
            lines.append("  %s  %s" % (e["id"], " ".join(cols)))
        text = "\n".join(lines)
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
