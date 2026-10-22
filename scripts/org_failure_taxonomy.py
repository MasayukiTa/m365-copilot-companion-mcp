# -*- coding: utf-8 -*-
"""Read-only: failure taxonomy and replay curriculum order for episode records (G1).

WHAT DECISION THIS SERVES. Which kinds of failure to replay first, from episode records already
written (rules and class definitions: relay/org/failure_taxonomy.py). It reads one JSON file,
prints the failure classes with counts and the replay order. It replays nothing, writes no file,
touches no .fleet directory, runs no git, never calls the network or the fleet, has no clock, and
no runtime module imports it.

Usage:
  python scripts/org_failure_taxonomy.py --records FILE.json [--format text|json]
FILE.json is a JSON list of episode records, or an object {"records": [...]}.

Exit code: 0 report printed, 2 refused (the file cannot be read, is not JSON, or has no record list).
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
    from relay.org import failure_taxonomy as T
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
    report = T.build_report(data)
    if args.format == "json":
        text = T.to_json(report)
    else:
        lines = ["failure taxonomy: %d records, %d failures, %d successes, %d no outcome, %d malformed"
                 % (report["total"], report["failures"], report["successes"],
                    report["no_outcome"], report["malformed"])]
        for c in report["curriculum"]:
            lines.append("  replay %d: %s x%d (latest ts %s)"
                         % (c["rank"], c["class"], c["count"], c["latest_ts"]))
            for eid in c["episode_ids"]:
                lines.append("    episode %s" % (eid or "(no id)"))
        text = "\n".join(lines)
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
