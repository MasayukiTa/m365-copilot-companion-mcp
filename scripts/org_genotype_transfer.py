# -*- coding: utf-8 -*-
"""Read-only: run the genotype transfer test and print its report as JSON (slice E6).

WHAT DECISION THIS SERVES. Whether a candidate that beat its parent in the context it was found in
(one model, one task kind) still beats it in other models and task kinds (see
relay/org/genotype_transfer.py). It submits no job, touches nothing under .fleet, and no runtime
module imports it. The only file it ever writes is the one named by --out.

Usage:  python scripts/org_genotype_transfer.py --max-contexts 4 --max-candidates 6
            --context model:alpha:home --context model:beta --context kind:review
            [--candidates-json FILE] [--parent-json FILE] [--seed 0] [--evaluator MODULE:FUNCTION]
            [--min-gain X] [--tolerance X] [--other-share X] [--min-other-contexts N]
            [--holdout H.json --seal SEAL.json] [--dev DEV.json] [--exposures EX.json]
            [--holdout-budget N] [--out REPORT.json]

--context is AXIS:NAME or AXIS:NAME:home (AXIS is model or kind) and may be repeated; at least one
must be home. Without --candidates-json the candidates are the single-gene neighbours of the
parent (genotype_candidates, drawn with --seed); the file may hold a list of genes dicts or of
candidate records. Without --evaluator a deterministic synthetic evaluator is used (a dry run: the
scores mean nothing). MODULE:FUNCTION must be a callable evaluate(genes, context_name) ->
{"score": number}. Without --seal every candidate is rejected as "holdout unsealed".
Exit code: 0 done, 2 refused (bad input).
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def _load(path):
    with open(path, encoding="utf-8-sig") as fh:
        return json.loads(fh.read())


def _evaluator(spec, T):
    if not spec:
        return T.dry_run_evaluator()
    mod, _, name = spec.partition(":")
    if not mod or not name:
        raise T.TransferError("--evaluator needs MODULE:FUNCTION")
    return getattr(importlib.import_module(mod), name)


def main(argv=None):
    from relay.org import genotype as G
    from relay.org import genotype_candidates as C
    from relay.org import genotype_holdout as H
    from relay.org import genotype_transfer as T
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--max-contexts", type=int, required=True)
    ap.add_argument("--max-candidates", type=int, required=True)
    ap.add_argument("--context", action="append", default=[])
    ap.add_argument("--candidates-json", default="")
    ap.add_argument("--parent-json", default="")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--evaluator", default="")
    ap.add_argument("--min-gain", type=float, default=T.DEFAULT_MIN_GAIN)
    ap.add_argument("--tolerance", type=float, default=T.DEFAULT_TOLERANCE)
    ap.add_argument("--other-share", type=float, default=T.DEFAULT_OTHER_SHARE)
    ap.add_argument("--min-other-contexts", type=int, default=T.DEFAULT_MIN_OTHER_CONTEXTS)
    ap.add_argument("--holdout")
    ap.add_argument("--seal")
    ap.add_argument("--dev")
    ap.add_argument("--exposures")
    ap.add_argument("--holdout-budget", type=int, default=H.DEFAULT_BUDGET)
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)
    try:
        if args.out and ".fleet" in os.path.normpath(os.path.abspath(args.out)).split(os.sep):
            raise T.TransferError("--out must not be under .fleet")
        if args.parent_json:
            data = _load(args.parent_json)
            parent = data.get("genes", data) if isinstance(data, dict) else data
        else:
            parent = G.current()
        if args.candidates_json:
            cands = _load(args.candidates_json)
        else:
            cands = C.generate(parent, args.max_candidates, seed=args.seed)
        report = T.transfer(
            parent, cands, [T.parse_context(c) for c in args.context],
            _evaluator(args.evaluator, T), args.max_contexts, args.max_candidates,
            min_gain=args.min_gain, tolerance=args.tolerance, other_share=args.other_share,
            min_other_contexts=args.min_other_contexts,
            sealed=_load(args.seal) if args.seal else None,
            dev_ids=_load(args.dev) if args.dev else [],
            holdout_ids=_load(args.holdout) if args.holdout else [],
            exposures=_load(args.exposures) if args.exposures else [],
            budget=args.holdout_budget, harness_id=args.evaluator or "dry-run")
    except (OSError, ImportError, AttributeError, ValueError, TypeError, KeyError) as exc:
        sys.stderr.write("refused: %s\n" % exc)
        return 2
    text = T.to_json(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
