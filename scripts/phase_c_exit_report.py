# -*- coding: utf-8 -*-
"""Phase C exit verification (roadmap slice C8): a PAIRED static-baseline vs dynamic-coordination
comparison of quality and cost, BROKEN DOWN BY TASK KIND, from the telemetry that already exists
under .fleet. Read-only: it schedules nothing, submits nothing, and writes only its own report
file (--out). It is a SHADOW report -- running it changes no behaviour anywhere.

WHAT DECISION THIS SERVES. Phase C's exit condition asks whether turning on dynamic coordination
(the router / fan-out acting on its own choice) beats a static baseline (queue order, no acting)
ON THE SAME GOALS, and whether enough such pairs exist yet to say so per task kind. This script
answers the measurable half: for each task kind it pairs a baseline (A) run with a coordinated (B)
run of the SAME goal, reports the paired quality/cost difference with a count and an interval, and
prints ONE explicit line naming exactly which pairs are still missing to reach the plan's exit
condition (--min-pairs per kind). Whether to DECLARE Phase C done from that is the operator's call.

WHY REUSE, NOT REINVENT. The per-run outcome measures (success, turns, wall_s, tool_calls,
retries) and the C0 pairing (one entry per (goal, condition); a goal is a complete pair only when
both sides are settled and carry the SAME experiment id) come verbatim from scripts/ab_compare.py.
The only thing added here is the TASK-KIND axis and the missing-pairs line, so the numbers are the
same numbers C0/C3 already defend, just grouped by kind. The paired comparison this exits on is
the one scripts/load_routing_pairwise.py (merged C3 step 2) built; this script reuses that module
so C8 reports exactly the pairs C3 would count, grouped by kind.

MANIFEST (JSON array, {"entries": [...]}, or JSONL) -- the SAME shape ab_compare.py accepts, with
ONE extra optional field per entry:
    {"goal_id": "g1", "condition": "A", "experiment_id": "exp-...",
     "campaign_id": "c84ec492b5b7e", "task_kind": "refactor"}
  condition   "A" is the static baseline (queue order). "B" is dynamic coordination (router acts).
  task_kind   the kind of task this goal is (e.g. "refactor", "bugfix", "doc"). Optional; an entry
              without it is grouped under "(unspecified)". Both sides of a pair SHOULD declare the
              same kind; if they differ, the pair is reported as a kind mismatch and excluded, so a
              baseline and a coordinated run of different kinds are never silently compared.

When --use-router-action is given (default on), a B side is additionally required to have genuinely
reordered admission, exactly as load_routing_pairwise.py checks it (one mode=on differs=true shadow
row for that run); a baseline A side must NOT have reordered. Pass --no-router-action to pair on
settled+experiment_id alone (plain C0), for comparisons that are not about the load router.

MEASURES (per run, identical to scripts/ab_compare.py, from the same .fleet files, read-only):
success (quality), and turns / wall_s / tool_calls / retries (cost). Deltas are B minus A per goal,
only for metrics present on both sides. QUALITY is success; COST is the other four.

INTERVAL. For each task kind and metric, the paired differences (B-A) get a mean and a 95%
confidence interval on that mean (Student t for n>=2, from the sample sd of the paired diffs). With
n<2 the interval is null and the text says so. As in ab_compare/C0 there is no claim beyond the
interval: a handful of pairs shows which way and how tightly the numbers lean, not a proof.

EXIT CONDITION / MISSING PAIRS. The plan's Phase C exit asks for at least --min-pairs complete
pairs per task kind. The report ends with an explicit MISSING line: for every kind that has fewer
than --min-pairs complete pairs, it names the kind, how many it has, and how many more it needs;
and it lists the goals whose pair is incomplete (with the reason) so an operator sees exactly which
runs to execute next. If every kind meets the bar the line says the exit condition is met.

  python scripts/phase_c_exit_report.py --manifest c8.json --out report.txt
         [--fleet-dir .fleet] [--min-pairs 5] [--no-router-action] [--format both|json|text]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from scripts import ab_compare as AB  # noqa: E402  (manifest parse + per-run outcome measures + C0 pairing)
from scripts import load_routing_pairwise as LP  # noqa: E402  (router-acted side check, merged C3 step 2)
from scripts import sibling_dup_report as S  # noqa: E402  (the telemetry loader ab_compare reads)

DEFAULT_MIN_PAIRS = 5
METRICS = AB.METRICS                       # ("success", "turns", "wall_s", "tool_calls", "retries")
QUALITY = "success"
COST = tuple(m for m in METRICS if m != QUALITY)
UNSPECIFIED = "(unspecified)"
MET, UNMET = "EXIT_CONDITION_MET", "EXIT_CONDITION_UNMET"
NOTE = ("Shadow report: read-only, changes no behaviour. Per kind the paired difference (B-A, "
        "dynamic minus static) is shown with a 95% interval; with few pairs this shows which way "
        "and how tightly the numbers lean, not a proof.")

# 95% two-sided Student-t critical values by degrees of freedom (n-1); df>=30 uses the normal 1.96.
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306,
        9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120,
        17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064,
        25: 2.060, 26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045}


def _t95(df):
    if df <= 0:
        return None
    return _T95.get(df, 1.96)


# --------------------------------------------------------------------------- manifest with task_kind

def load_manifest(path):
    """ab_compare.load_manifest, plus a task_kind per entry (default UNSPECIFIED). Reusing the base
    loader keeps the exact same validation and problem reporting; we only re-attach the kind, which
    the base loader drops."""
    with open(path, "r", encoding="utf-8-sig") as fh:
        raw = AB._parse_manifest_text(fh.read())
    kinds = {}
    for e in raw:
        if isinstance(e, dict) and "_unparseable" not in e:
            g = str(e.get("goal_id") or "").strip()
            c = str(e.get("condition") or "").strip().upper()
            k = str(e.get("task_kind") or "").strip() or UNSPECIFIED
            if g and c:
                kinds[(g, c)] = k
    entries, problems = AB.load_manifest(path)
    for key, entry in entries.items():
        entry["task_kind"] = kinds.get(key, UNSPECIFIED)
    return entries, problems


# --------------------------------------------------------------------------- interval on paired diffs

def _mean_ci(vals):
    """(mean, lo, hi, n) for a list of paired differences. 95% t-interval on the mean when n>=2;
    lo/hi are None when n<2 (no spread to speak of). None entries are dropped first."""
    xs = [float(v) for v in vals if v is not None]
    n = len(xs)
    if n == 0:
        return None, None, None, 0
    mean = sum(xs) / n
    if n < 2:
        return mean, None, None, n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    sd = math.sqrt(var)
    t = _t95(n - 1)
    half = t * sd / math.sqrt(n) if t is not None else None
    if half is None:
        return mean, None, None, n
    return mean, mean - half, mean + half, n


# --------------------------------------------------------------------------- the comparison by kind

def _pair_ok(a, b, use_router_action, action):
    """(ok, reason). C0 pairing (both settled, same experiment id), plus -- when use_router_action --
    load_routing_pairwise's check that B genuinely reordered admission and A did not. Also rejects a
    pair whose two sides declare different task kinds."""
    if a["task_kind"] != b["task_kind"]:
        return False, "task_kind differs between sides (A=%s, B=%s)" % (a["task_kind"], b["task_kind"])
    if use_router_action:
        a_ok, a_why = LP._label_side(a, action)
        b_ok, b_why = LP._label_side(b, action)
        if not (a_ok and b_ok):
            return False, a_why or b_why
    if a["status"] != "settled" or b["status"] != "settled":
        return False, "A %s, B %s" % (a["status"], b["status"])
    if a["experiment_id"] != b["experiment_id"]:
        return False, "experiment_id differs between sides"
    return True, ""


def compare(entries, events, workers, camp_rows, action, min_pairs=DEFAULT_MIN_PAIRS,
            use_router_action=True):
    """Pair A (static baseline) with B (dynamic coordination) per goal, exactly as C0/C3 do, then
    group the complete pairs by task kind and summarise quality and cost per kind with an interval.
    Every goal that is not a complete pair is listed with a reason."""
    idx = AB._index(events, workers, camp_rows)
    recs = {k: dict(AB.measure(e, idx), task_kind=e.get("task_kind", UNSPECIFIED))
            for k, e in entries.items()}
    pairs_by_kind, incomplete = {}, []
    for goal in sorted({g for g, _ in recs}):
        a, b = recs.get((goal, "A")), recs.get((goal, "B"))
        if a is None or b is None:
            incomplete.append({"goal_id": goal, "task_kind": (a or b or {}).get("task_kind",
                               UNSPECIFIED), "reason": "only one condition in manifest"})
            continue
        ok, why = _pair_ok(a, b, use_router_action, action)
        if not ok:
            incomplete.append({"goal_id": goal, "task_kind": a["task_kind"], "reason": why})
            continue
        delta = {m: (b[m] - a[m]) if a[m] is not None and b[m] is not None else None
                 for m in METRICS}
        pairs_by_kind.setdefault(a["task_kind"], []).append({
            "goal_id": goal, "experiment_id": a["experiment_id"], "task_kind": a["task_kind"],
            "A": {m: a[m] for m in METRICS}, "B": {m: b[m] for m in METRICS},
            "delta_B_minus_A": delta})
    # per-kind summary with interval, plus the missing count against the exit bar
    kinds = {}
    all_kinds = sorted(set(pairs_by_kind) | {g["task_kind"] for g in incomplete})
    for kind in all_kinds:
        ps = pairs_by_kind.get(kind, [])
        summary = {}
        for m in METRICS:
            mean, lo, hi, n = _mean_ci([p["delta_B_minus_A"][m] for p in ps])
            summary[m] = {"mean": mean, "ci95_low": lo, "ci95_high": hi, "n": n}
        have = len(ps)
        kinds[kind] = {
            "complete_pairs": have,
            "missing_to_exit": max(0, min_pairs - have),
            "meets_exit": have >= min_pairs,
            "quality_delta": summary[QUALITY],
            "cost_delta": {m: summary[m] for m in COST},
            "pairs": ps,
        }
    total_pairs = sum(k["complete_pairs"] for k in kinds.values())
    unmet = sorted(k for k, v in kinds.items() if not v["meets_exit"])
    verdict = MET if (kinds and not unmet) else UNMET
    return {
        "verdict": verdict, "min_pairs": min_pairs, "use_router_action": use_router_action,
        "total_complete_pairs": total_pairs, "kinds": kinds,
        "kinds_meeting_exit": sorted(k for k, v in kinds.items() if v["meets_exit"]),
        "kinds_missing": unmet, "incomplete_goals": incomplete, "note": NOTE,
        "experiment_ids": sorted({e["experiment_id"] for e in entries.values()}),
    }


# --------------------------------------------------------------------------- output

def _ci(d):
    if d["mean"] is None:
        return "n/a (n=0)"
    if d["ci95_low"] is None:
        return "%s (n=%d, no interval)" % (AB._f(d["mean"]), d["n"])
    return "%s [%s, %s] (n=%d)" % (AB._f(d["mean"]), AB._f(d["ci95_low"]),
                                   AB._f(d["ci95_high"]), d["n"])


def missing_line(rep):
    """The one explicit line the task asks for: exactly which pairs are missing to reach the exit
    condition. Always returned (as a string), whether or not the bar is met."""
    if not rep["kinds"]:
        return "MISSING: no task kinds in manifest; need >=%d complete pairs per kind to exit." \
            % rep["min_pairs"]
    if rep["verdict"] == MET:
        return "MISSING: none. Every task kind has >=%d complete pairs; exit condition met." \
            % rep["min_pairs"]
    parts = []
    for kind in rep["kinds_missing"]:
        v = rep["kinds"][kind]
        goals = [g["goal_id"] for g in rep["incomplete_goals"] if g["task_kind"] == kind]
        gtxt = (" incomplete goals: " + ", ".join(sorted(goals))) if goals else ""
        parts.append("%s need %d more (have %d of %d).%s"
                     % (kind, v["missing_to_exit"], v["complete_pairs"], rep["min_pairs"], gtxt))
    return "MISSING: " + " | ".join(parts)


def render_text(rep):
    lines = ["# Phase C exit verification: %s (%d task kind(s), %d complete pairs, need >=%d per "
             "kind)" % (rep["verdict"], len(rep["kinds"]), rep["total_complete_pairs"],
                        rep["min_pairs"]),
             "# A is the static baseline (queue order); B is dynamic coordination. Delta is B-A "
             "(dynamic minus static): for cost, negative means dynamic is cheaper.",
             "# router-acted check: %s" % ("on (B must have reordered admission; A must not)"
                                           if rep["use_router_action"] else "off (plain C0 pairing)"),
             "# " + rep["note"]]
    for kind in sorted(rep["kinds"]):
        v = rep["kinds"][kind]
        q = v["quality_delta"]
        lines.append("# [%s] %d complete pair(s)%s -- quality (success) delta %s"
                     % (kind, v["complete_pairs"],
                        " (meets exit)" if v["meets_exit"] else " (below exit)", _ci(q)))
        lines.append("#   cost delta: " + ", ".join(
            "%s %s" % (m, _ci(v["cost_delta"][m])) for m in COST))
    if rep["incomplete_goals"]:
        lines.append("# incomplete goals: %d (%s)" % (len(rep["incomplete_goals"]), "; ".join(
            "%s[%s]: %s" % (g["goal_id"], g["task_kind"], g["reason"])
            for g in rep["incomplete_goals"][:5])))
    for p in rep.get("problems", []):
        lines.append("# manifest problem: " + p)
    lines.append("# " + missing_line(rep))
    return "\n".join(lines) + "\n"


def build_report(manifest, fleet_dir, min_pairs=DEFAULT_MIN_PAIRS, use_router_action=True):
    """Load everything read-only and return the report dict (with any manifest problems attached)."""
    entries, problems = load_manifest(manifest)
    events, workers, camps, _n = S.load_fleet(fleet_dir)
    action = LP.router_action_by_run(fleet_dir) if use_router_action else {}
    rep = compare(entries, events, workers, camps, action, min_pairs=min_pairs,
                  use_router_action=use_router_action)
    rep["problems"] = problems
    return rep


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Phase C exit verification: paired static-baseline vs dynamic-coordination "
                    "comparison of quality and cost per task kind (read-only shadow report).")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", default=None,
                    help="write the report here (the only file this script writes). Omit to only "
                         "print to stdout.")
    ap.add_argument("--fleet-dir", default=None,
                    help="directory holding the fleet records (default: the live one, read-only)")
    ap.add_argument("--min-pairs", type=int, default=DEFAULT_MIN_PAIRS,
                    help="exit condition: complete pairs required per task kind (default 5)")
    ap.add_argument("--no-router-action", dest="use_router_action", action="store_false",
                    help="pair on settled+experiment_id alone (plain C0), not the load router")
    ap.add_argument("--format", choices=("both", "json", "text"), default="both")
    ap.set_defaults(use_router_action=True)
    args = ap.parse_args(argv)
    fleet_dir = args.fleet_dir or os.path.join(REPO, ".fleet")
    rep = build_report(args.manifest, fleet_dir, min_pairs=args.min_pairs,
                       use_router_action=args.use_router_action)
    out = []
    if args.format in ("both", "json"):
        out.append(json.dumps(rep, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    if args.format in ("both", "text"):
        out.append(render_text(rep))
    text = "".join(out)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.write(text.encode("ascii", "replace").decode("ascii"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
