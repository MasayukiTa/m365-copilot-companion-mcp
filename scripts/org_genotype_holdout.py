# -*- coding: utf-8 -*-
"""Read-only: check a sealed holdout for contamination, or print its seal (slice E7).

WHAT DECISION THIS SERVES. Whether a holdout score can still be trusted (see
relay/org/genotype_holdout.py). It reads JSON files named on the command line and prints; it writes
no file, submits no job and no runtime module imports it.

Usage:
  python scripts/org_genotype_holdout.py --holdout HOLDOUT.json --make-seal
  python scripts/org_genotype_holdout.py --holdout HOLDOUT.json --seal SEAL.json
         [--dev DEV.json] [--exposures EXPOSURES.json] [--add-exposure ITEM:GENOTYPE:PURPOSE:AT]
         [--budget N] [--format text|json]

HOLDOUT.json and DEV.json are lists of item ids; EXPOSURES.json is a list of
{"item", "genotype", "purpose", "at"}; SEAL.json is the output of --make-seal.
Exit code: 0 clean (or seal printed), 1 contaminated, 3 unsealed, 2 refused (bad input).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

EXIT = {"clean": 0, "contaminated": 1, "unsealed": 3}


def _load(path):
    with open(path, encoding="utf-8-sig") as fh:
        return json.loads(fh.read())


def main(argv=None):
    from relay.org import genotype_holdout as H
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--holdout", required=True)
    ap.add_argument("--seal")
    ap.add_argument("--dev")
    ap.add_argument("--exposures")
    ap.add_argument("--add-exposure", action="append", default=[],
                    metavar="ITEM:GENOTYPE:PURPOSE:AT")
    ap.add_argument("--budget", type=int, default=H.DEFAULT_BUDGET)
    ap.add_argument("--make-seal", action="store_true")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    try:
        holdout = _load(args.holdout)
        if args.make_seal:
            text = H.to_json(H.seal(holdout))
            sys.stdout.buffer.write((text + "\n").encode("utf-8"))
            return 0
        sealed = _load(args.seal) if args.seal else None
        dev = _load(args.dev) if args.dev else []
        exposures = _load(args.exposures) if args.exposures else []
        if not isinstance(exposures, list):
            raise H.HoldoutError("exposures must be a list")
        for spec in args.add_exposure:  # what-if: judge the planned use before running it
            parts = spec.split(":", 3)
            if len(parts) != 4:
                raise H.HoldoutError("--add-exposure needs ITEM:GENOTYPE:PURPOSE:AT")
            exposures = H.record_exposure(exposures, *parts)
        v = H.verdict(sealed, dev, holdout, exposures, budget=args.budget)
    except (OSError, ValueError) as exc:  # HoldoutError and JSON errors are ValueErrors
        sys.stderr.write("refused: %s\n" % exc)
        return 2
    if args.format == "json":
        text = H.to_json(v)
    else:
        f = v["findings"]
        lines = ["state: %s" % v["state"],
                 "holdout items: %d  budget: %d" % (v["holdout_count"], v["budget"]),
                 "digest mismatch: %s" % ("yes" if f["digest_mismatch"] else "no")]
        for i in f["overlap"]:
            lines.append("  overlap with dev: %s" % i)
        for x in f["leaked"]:
            lines.append("  leaked: %s (%s)" % (x["item"], ",".join(x["purposes"])))
        for x in f["over_budget"]:
            lines.append("  over budget: %s (%d genotypes)" % (x["item"], len(x["genotypes"])))
        text = "\n".join(lines)
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    return EXIT[v["state"]]


if __name__ == "__main__":
    sys.exit(main())
