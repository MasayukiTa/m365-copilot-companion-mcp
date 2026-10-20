# -*- coding: utf-8 -*-
"""Read-only report: what shadow evaluation says about the change proposals on file (slice D4).

WHAT DECISION THIS SERVES. Whether a proposal for a role or protocol text (D3) has enough
behind it to be worth a live organisation A/B (D5), and how much data that A/B would need.

WHAT IT CANNOT SAY. Shadow evaluation records, per run, which proposals would have touched it
(.fleet/org_shadow_eval.jsonl, written when the setting `org_shadow_eval` is `shadow`). A run
took one path, the one under the definition in force. There is no outcome for the path under a
proposed definition, so this report can NOT show that a proposal is better or worse. It counts
how many runs each proposal would have touched, and shows the BASELINE outcome of those runs
(success rate and mean turns) per (role, kind of work, org version), joined by run id from the
records the fleet already keeps (status.json, history.json). Records without a run id (the
campaign ledger's children and merges) cannot be joined and are counted as such, not guessed.

WHAT A PROMOTION DECISION NEEDS. For each cell with a baseline success rate p, the number of runs
PER ARM an A/B needs to see a difference of `--delta` (default 0.15) in the success rate at 5%
two-sided significance and 80% power: n = 2 (1.96 + 0.84)^2 p(1-p) / delta^2 with p the mean of
the baseline rate and the rate plus delta, kept within [0.05, 0.95]. The proposed arm exists only
in an A/B (D5), so the shortfall is shown against the baseline runs seen so far and an estimate
of the days at the observed rate of touched runs.

It writes nothing and applies nothing.

  python scripts/org_shadow_eval_report.py [--fleet-dir .fleet] [--min-n 5] [--delta 0.15]
                                           [--format text|json]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

Z_ALPHA, Z_BETA = 1.96, 0.84
DEFAULT_DELTA = 0.15
NOTE = ("Shadow cannot show a proposal is better: the path under the proposed definition was "
        "not taken and has no outcome. This counts the runs a proposal would have touched and "
        "shows their baseline outcome.")
_ATTEMPT = re.compile(r"_a\d+$")


def runs_needed(p, delta=DEFAULT_DELTA):
    """Runs per arm to see a success-rate difference of `delta` from baseline rate `p`
    (two-sided 5%, power 80%). `p` is clamped to [0.05, 0.95]; the result is at least 1."""
    p0 = min(max(float(p), 0.05), 0.95)
    p1 = min(p0 + delta, 0.99)
    pbar = (p0 + p1) / 2.0
    d = max(p1 - p0, 1e-6)
    return max(1, int(math.ceil(2 * (Z_ALPHA + Z_BETA) ** 2 * pbar * (1 - pbar) / (d * d))))


def _base(run_id):
    return _ATTEMPT.sub("", str(run_id or ""))


def _cells(recs, org_runs, org_label):
    cells = {}
    for r in recs:
        outcome = str(r.get("outcome") or "").upper()
        if not outcome:
            continue
        pair = org_runs.get(_base(r.get("run_id")))
        key = (str(r.get("role") or "(unnamed)"), str(r.get("kind") or "unknown"),
               org_label(pair) if pair else "(not recorded)")
        c = cells.setdefault(key, {"outcomes": [], "turns": []})
        c["outcomes"].append(outcome)
        t = r.get("turn")
        if isinstance(t, (int, float)) and not isinstance(t, bool):
            c["turns"].append(t)
    return cells


def build(fleet_dir, min_n=5, delta=DEFAULT_DELTA):
    """The whole report as a dict. Read-only; a missing or damaged file leaves its part empty."""
    from relay.org import proposals, shadow_eval
    from relay import task_kind as TK
    from scripts import capability_profile as CP
    from scripts import sibling_dup_report as S

    props, bad_prop = proposals.read(os.path.join(fleet_dir, proposals.FILENAME))
    touches, bad_touch = shadow_eval.load(os.path.join(fleet_dir, shadow_eval.FILENAME))
    try:
        current = proposals.targets()
    except Exception:
        current = {}
    camps, bad_ca = CP._read_jsonl_counted(os.path.join(fleet_dir, "campaigns.jsonl"))
    history, bad_hi = CP._read_history(os.path.join(fleet_dir, "history.json"))
    workers = S._read_status_workers(os.path.join(fleet_dir, "status.json"))
    records = CP.build_records(workers, camps, history, {}, TK.task_kind)
    joinable = [r for r in records if r.get("run_id")]
    by_base = {}
    for r in joinable:
        by_base.setdefault(_base(r["run_id"]), []).append(r)
    org_runs, _defs, bad_org = CP.load_org(fleet_dir)

    out = []
    for p in props:
        pid = shadow_eval.proposal_id(p["target"], p["base_hash"], p["proposed_hash"])
        runs = touches.get(pid, {})
        bases = {_base(k) for k in runs}
        roles = shadow_eval.TARGET_ROLES.get(p["target"])
        recs = [r for b in bases for r in by_base.get(b, [])
                if roles is None or str(r.get("role") or "").lower() in roles]
        with_outcome = {_base(r["run_id"]) for r in recs if r.get("outcome")}
        stamps = sorted(runs.values())
        span_days = (stamps[-1] - stamps[0]) / 86400.0 if len(stamps) > 1 else 0.0
        per_day = (len(bases) / span_days) if span_days >= 1.0 else None
        cells = []
        for (role, kind, org), c in sorted(_cells(recs, org_runs, CP.org_label).items()):
            n = len(c["outcomes"])
            rate = CP._success_rate(c["outcomes"])
            need = runs_needed(rate, delta) if rate is not None else None
            short = max(0, need - n) if need is not None else None
            days = (short / per_day) if (short and per_day) else (0.0 if short == 0 else None)
            cells.append({"role": role, "kind": kind, "org_version": org, "n": n,
                          "enough_to_read": n >= min_n,
                          "success_rate": rate,
                          "mean_turns": (sum(c["turns"]) / len(c["turns"])) if c["turns"] else None,
                          "runs_needed_per_arm": need, "shortfall_baseline_runs": short,
                          "days_at_observed_rate": days})
        out.append({"proposal": pid, "target": p["target"], "base_hash": p["base_hash"],
                    "proposed_hash": p["proposed_hash"], "author": p["author"],
                    "stale": current.get(p["target"], "") != p["base_hash"],
                    "runs_would_touch": len(bases), "runs_with_outcome": len(with_outcome),
                    "runs_without_outcome": len(bases) - len(with_outcome),
                    "touched_runs_per_day": per_day, "cells": cells})
    unreadable = {k: v for k, v in (("org_proposals.jsonl", bad_prop),
                                    ("org_shadow_eval.jsonl", bad_touch),
                                    ("campaigns.jsonl", bad_ca), ("history.json", bad_hi),
                                    ("org_versions.jsonl", bad_org)) if v}
    return {"note": NOTE, "proposals": out, "count": len(out),
            "records_without_run_id": len(records) - len(joinable),
            "thresholds": {"min_n": min_n, "delta": delta, "alpha": 0.05, "power": 0.8},
            "unreadable": unreadable}


def _f(v, nd=2):
    return "n/a" if v is None else ("%.*f" % (nd, v))


def render_text(rep):
    lines = ["# Shadow evaluation of change proposals", "# " + rep["note"],
             "%d proposal(s) on file; %d record(s) carry no run id and cannot be joined"
             % (rep["count"], rep["records_without_run_id"])]
    if rep["unreadable"]:
        lines.append("# SKIPPED UNREADABLE LINES: " + ", ".join(
            "%s %d" % kv for kv in sorted(rep["unreadable"].items())))
    for p in rep["proposals"]:
        lines.append("- %s  %s  %s -> %s  by %s%s" % (
            p["proposal"], p["target"], p["base_hash"], p["proposed_hash"], p["author"],
            "  STALE (definition changed; no new runs are counted)" if p["stale"] else ""))
        lines.append("    would have touched %d run(s); %d with a recorded outcome, %d without; "
                     "touched runs per day %s" % (p["runs_would_touch"], p["runs_with_outcome"],
                                                  p["runs_without_outcome"],
                                                  _f(p["touched_runs_per_day"], 1)))
        for c in p["cells"]:
            lines.append("    %-11s %-12s %-40s runs %4d success %s turns %s%s | A/B needs %s per "
                         "arm, baseline short by %s, ~%s days" % (
                             c["role"], c["kind"], c["org_version"], c["n"],
                             _f(c["success_rate"]), _f(c["mean_turns"], 1),
                             "" if c["enough_to_read"] else " (too few to read)",
                             c["runs_needed_per_arm"], c["shortfall_baseline_runs"],
                             _f(c["days_at_observed_rate"], 1)))
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fleet-dir", default=os.path.join(REPO, ".fleet"))
    ap.add_argument("--min-n", type=int, default=5)
    ap.add_argument("--delta", type=float, default=DEFAULT_DELTA)
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    rep = build(args.fleet_dir, args.min_n, args.delta)
    out = json.dumps(rep, ensure_ascii=False, indent=1) + "\n" if args.format == "json" \
        else render_text(rep)
    try:
        sys.stdout.write(out)
    except UnicodeEncodeError:
        sys.stdout.write(out.encode("ascii", "replace").decode("ascii"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
