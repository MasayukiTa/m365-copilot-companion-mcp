# -*- coding: utf-8 -*-
"""Read-only: check that an Evolution Lab directory is fully separate from production (slice F1).

WHAT DECISION THIS SERVES. Whether a lab directory may be used (see relay/org/lab_isolation.py for
the rules and the reason for each). It only reads paths and the LAB_COPY.json marker and prints; it
creates no directory, writes no file, submits no job and no runtime module imports it.

Usage:
  python scripts/org_lab_isolation.py --lab LAB_ROOT --production-root PROD_ROOT
         --production-state PROD_STATE_DIR [--lab-state LAB_STATE_DIR] [--format text|json]
  python scripts/org_lab_isolation.py --print-marker COMMIT:CREATED_AT
         (prints the text for LAB_COPY.json to stdout; you save it yourself)

Exit code: 0 ok, 1 violations, 2 refused (bad input).
"""
from __future__ import annotations

import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def main(argv=None):
    from relay.org import lab_isolation as L
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--lab")
    ap.add_argument("--production-root")
    ap.add_argument("--production-state")
    ap.add_argument("--lab-state")
    ap.add_argument("--print-marker", metavar="COMMIT:CREATED_AT")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    try:
        if args.print_marker:
            commit, sep, created = args.print_marker.partition(":")
            if not sep:
                raise L.LabIsolationError("--print-marker needs COMMIT:CREATED_AT")
            text = L.to_json(L.make_marker(commit, created))
            sys.stdout.buffer.write((text + "\n").encode("utf-8"))
            return 0
        if not (args.lab and args.production_root and args.production_state):
            raise L.LabIsolationError(
                "--lab, --production-root and --production-state are required")
        marker, err = L.read_marker(args.lab)
        v = L.check_lab_layout(args.lab, args.production_root, args.production_state,
                               lab_state_dir=args.lab_state, marker=marker, marker_error=err)
    except (OSError, ValueError) as exc:
        sys.stderr.write("refused: %s\n" % exc)
        return 2
    if args.format == "json":
        text = L.to_json(v)
    else:
        lines = ["lab layout: %s" % ("ok" if v["ok"] else "VIOLATIONS"),
                 "lab root: %s" % v["lab_root"], "lab state: %s" % v["lab_state_dir"]]
        for x in v["violations"]:
            lines.append("  %s: %s" % (x["code"], x["detail"]))
        text = "\n".join(lines)
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    return 0 if v["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
