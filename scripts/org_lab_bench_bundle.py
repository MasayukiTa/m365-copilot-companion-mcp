# -*- coding: utf-8 -*-
"""Read-only: check a reproducible bench bundle for Evolution Lab experiments (slice F4).

WHAT DECISION THIS SERVES. Whether a benchmark result can be trusted to belong to the bundle it
names, and whether two bundles measure the same thing so their results may be compared (rules and
reasons: relay/org/lab_bench_bundle.py). It reads JSON files, prints findings, writes no file,
runs no benchmark, runs no git, has no clock and no runtime module imports it.

Usage:
  python scripts/org_lab_bench_bundle.py --check --bundle FILE.json [--compare OTHER.json]
                                         [--format text|json]
  python scripts/org_lab_bench_bundle.py --hash --bundle FILE.json
  python scripts/org_lab_bench_bundle.py --build --bundle SPEC.json
--check validates one bundle (and, with --compare, whether the two are comparable). --hash prints
the bundle_hash the content should carry, so a hand-written spec can be given its name.

Exit code: 0 ok, 1 violations or not comparable, 2 refused (bad input).
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
    from relay.org import lab_bench_bundle as B
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--hash", action="store_true")
    mode.add_argument("--build", action="store_true", help="print the spec with its bundle_hash filled in")
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--compare", default=None, help="a second bundle to compare with (--check)")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    try:
        bundle = _load(args.bundle)
        if args.build:
            sys.stdout.buffer.write((B.to_json(B.build_bundle(bundle)) + "\n").encode("utf-8"))
            return 0
        if args.hash:
            sys.stdout.buffer.write((B.compute_hash(bundle) + "\n").encode("utf-8"))
            return 0
        report = B.check_bundle(bundle)
        if args.compare is not None:
            report["compare"] = B.compare_bundles(bundle, _load(args.compare))
    except (OSError, ValueError) as exc:
        sys.stderr.write("refused: %s\n" % exc)
        return 2
    cmp_ = report.get("compare")
    ok = report["ok"] and (cmp_ is None or cmp_["comparable"])
    if args.format == "json":
        text = B.to_json(report)
    else:
        lines = ["lab bench bundle: %s" % ("ok" if report["ok"] else "NOT OK")]
        for x in report["violations"]:
            lines.append("  violation %s [%s]: %s" % (x["code"], x["id"], x["detail"]))
        if cmp_ is not None:
            lines.append("comparable: %s" % ("yes" if cmp_["comparable"] else "NO"))
            if cmp_["differences"]:
                lines.append("  differs in: %s" % ", ".join(cmp_["differences"]))
            if cmp_["invalid"]:
                lines.append("  fails its own check: %s" % ", ".join(cmp_["invalid"]))
        text = "\n".join(lines)
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
