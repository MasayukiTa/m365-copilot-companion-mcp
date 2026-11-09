# -*- coding: utf-8 -*-
"""Read-only PAIRED comparison for load-aware routing (C3 step 2): did acting on the router's
choice (`fanout_load_routing=on`) change outcome or cost against queue order (`off`/`shadow`), on
the SAME goals.

WHAT DECISION THIS SERVES, AND WHY A PAIRED TEST. scripts/load_routing_report.py reads the shadow
rows and can only say how OFTEN the router would have chosen differently; it says, in its own
words, that it cannot show the router would have done BETTER, because the goal it did not admit
was not run in its place, and it points at "a paired comparison of the two orders (C0)" as the
next step. This script is that step, specialised to load routing: it pairs an `on` run (the router
reordered admission) with an `off`/`shadow` run of the same goal (queue order), and measures the
outcome of each from the SAME existing telemetry scripts/ab_compare.py reads. It schedules
nothing, submits nothing and writes nothing; running the two conditions is the operator's job.

WHY NOT ab_compare.py ALONE. ab_compare pairs any two conditions by an experiment id, but it
cannot tell an `on` run in which the router actually reordered admission from one in which the
router AGREED with queue order (so `on` and `off` were byte-identical and there is nothing to
measure). The `on` mode writes one shadow row per admission with mode="on" and differs=true/false;
this script reads those rows per run and refuses to count a B side where the router never acted.
That guard is the whole reason this is a separate, small script and not a manifest for ab_compare.

MANIFEST (JSON array, {"entries": [...]}, or JSONL) -- the SAME shape ab_compare.py accepts, so an
operator can reuse one. One entry per (goal, condition):
    {"goal_id": "g1", "condition": "A", "experiment_id": "exp-...",
     "campaign_id": "c84ec492b5b7e", "run_id": "rA"}
  condition   "A" is the queue-order side (the router did NOT act: mode off or shadow).
              "B" is the router side (mode on). A pair whose sides are labelled against the mode
              the shadow rows record is rejected, so the two columns cannot be swapped by mistake.
  run_id      REQUIRED here (ab_compare lets campaign_id stand alone): the shadow rows are keyed by
              run_id, so without it this script cannot say whether the router acted on that side.
  experiment_id / campaign_id  as in ab_compare; a pair is compared only when both sides carry the
              SAME experiment id, and campaign_id (or the run's workers) give the outcome.

WHAT "THE ROUTER ACTED" MEANS (stated here, not buried in the logic).
  * A run's rows are the mode="on" shadow rows whose run_id is the entry's run_id.
  * The router ACTED on that run when at least one such row has differs=true: it admitted a goal
    other than the one queue order would have. A B side with on rows but no differs=true row is
    reported as `router_did_not_act` and its pair is incomplete: there is no routing effect there
    to measure, and counting it would dilute the very thing this test exists to see.
  * An A side is required to have NO mode="on" differs=true row for its run (it must be the
    queue-order side). An A side that actually reordered is a mislabelled pair and is rejected.

MEASURES (per run, identical to scripts/ab_compare.py, from campaigns.jsonl, status.json,
tool_events.jsonl in --fleet-dir, default the live .fleet, read-only): success, turns, wall_s,
tool_calls, retries. Deltas are B minus A per goal, only for metrics present on both sides.

A B side where the router never acted (no differs row) is not a complete pair at all: it is
reported as `router_did_not_act` and excluded, because there is no routing effect on that goal to
measure. So a complete pair ALWAYS has a B side that genuinely reordered admission.

VERDICT.
  INSUFFICIENT    complete pairs < --min-pairs (default 5). This also covers the case where the
                  router acted on too few goals, since a B side that never acted is not complete.
  DIRECTION_ONLY  otherwise. As in ab_compare there is NO significance test: with a handful of
                  goals the numbers show which way the conditions leaned, not that the difference
                  is real. The text says so every time.

  python scripts/load_routing_pairwise.py --manifest lr.json [--fleet-dir .fleet] [--min-pairs 5]
                                          [--format both|json|text]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from scripts import ab_compare as AB  # noqa: E402  (manifest shape + per-run outcome measures)
from scripts import load_routing_report as LRR  # noqa: E402  (the shadow-row reader + LOG_NAME)
from scripts import sibling_dup_report as S  # noqa: E402  (the telemetry loader ab_compare uses)

DEFAULT_MIN_PAIRS = 5
METRICS = AB.METRICS
NOTE = ("Direction-only: with this few goals the numbers show which way acting on the router's "
        "choice leaned, not that the difference is real. No significance is claimed.")

INSUFFICIENT, DIRECTION_ONLY = "INSUFFICIENT", "DIRECTION_ONLY"


# --------------------------------------------------------------------------- the shadow rows per run

def router_action_by_run(fleet_dir):
    """{run_id: {"on": bool, "acted": bool}} from the load-routing shadow log (and its rotated
    copy). `on` is True when any mode=="on" row carries that run_id; `acted` is True when any such
    row also has differs==True. Runs with no on rows do not appear. Never raises: a missing or
    unreadable log yields {}."""
    rows, _bad = LRR.read_rows(os.path.join(fleet_dir, LRR.LOG_NAME))
    by = {}
    for r in rows:
        if not isinstance(r, dict) or r.get("mode") != "on":
            continue
        rid = str(r.get("run_id") or "")
        if not rid:
            continue
        e = by.setdefault(rid, {"on": False, "acted": False})
        e["on"] = True
        if r.get("differs"):
            e["acted"] = True
    return by


def _label_side(entry, action):
    """(ok, note): does this entry's run match the side its condition claims?
    A (queue order): must NOT have acted. B (router): must have acted."""
    rid = str(entry.get("run_id") or "")
    act = action.get(rid, {"on": False, "acted": False})
    if entry["condition"] == "B":
        if not act["on"]:
            return False, "B side run %s has no mode=on shadow rows" % (rid or "(none)")
        if not act["acted"]:
            return False, "router_did_not_act: B side run %s never reordered admission" % rid
        return True, ""
    # A side: queue order. It must not have reordered.
    if act["acted"]:
        return False, "A side run %s actually reordered admission (mislabelled)" % rid
    return True, ""


# --------------------------------------------------------------------------- the comparison

def compare(entries, events, workers, camp_rows, action, min_pairs=DEFAULT_MIN_PAIRS):
    """Like ab_compare.compare, but each side is first checked against the router action its
    condition claims; a side that fails takes its goal out of the pairs with a stated reason."""
    idx = AB._index(events, workers, camp_rows)
    recs = {k: AB.measure(e, idx) for k, e in entries.items()}
    conditions = {}
    for cond in AB.CONDITIONS:
        mine = [r for (g, c), r in sorted(recs.items()) if c == cond]
        settled = [r for r in mine if r["status"] == "settled"]
        conditions[cond] = {
            "goals": len(mine), "settled": len(settled),
            "unsettled": sum(1 for r in mine if r["status"] == "unsettled"),
            "unknown_campaign": sum(1 for r in mine if r["status"] == "unknown_campaign"),
            "success_rate": AB._mean([r["success"] for r in settled]),
            "metrics": {m: {"mean": AB._mean([r[m] for r in settled]),
                            "n": sum(1 for r in settled if r[m] is not None)}
                       for m in METRICS[1:]},
        }
    pairs, incomplete = [], []
    for goal in sorted({g for g, _ in recs}):
        a, b = recs.get((goal, "A")), recs.get((goal, "B"))
        if a is None or b is None:
            incomplete.append({"goal_id": goal, "reason": "only one condition in manifest"})
            continue
        a_ok, a_why = _label_side(a, action)
        b_ok, b_why = _label_side(b, action)
        if not (a_ok and b_ok):
            incomplete.append({"goal_id": goal, "reason": a_why or b_why})
            continue
        if a["status"] != "settled" or b["status"] != "settled":
            incomplete.append({"goal_id": goal,
                               "reason": "A %s, B %s" % (a["status"], b["status"])})
            continue
        if a["experiment_id"] != b["experiment_id"]:
            incomplete.append({"goal_id": goal, "reason": "experiment_id differs between sides"})
            continue
        delta = {m: (b[m] - a[m]) if a[m] is not None and b[m] is not None else None
                 for m in METRICS}
        pairs.append({"goal_id": goal, "experiment_id": a["experiment_id"],
                      "A": {m: a[m] for m in METRICS}, "B": {m: b[m] for m in METRICS},
                      "delta_B_minus_A": delta})
    mean_delta = {m: {"mean": AB._mean([p["delta_B_minus_A"][m] for p in pairs]),
                      "n": sum(1 for p in pairs if p["delta_B_minus_A"][m] is not None)}
                  for m in METRICS}
    complete = len(pairs)
    verdict = INSUFFICIENT if complete < min_pairs else DIRECTION_ONLY
    return {
        "verdict": verdict, "complete_pairs": complete,
        "min_pairs": min_pairs, "note": NOTE, "conditions": conditions, "pairs": pairs,
        "mean_paired_delta_B_minus_A": mean_delta, "incomplete_goals": incomplete,
        "experiment_ids": sorted({e["experiment_id"] for e in entries.values()}),
    }


# --------------------------------------------------------------------------- output

def render_text(rep):
    lines = ["# Load-routing paired comparison: verdict %s (%d complete pairs, need %d)"
             % (rep["verdict"], rep["complete_pairs"], rep["min_pairs"]),
             "# A is queue order (router did not act); B is the router acting on its choice. A "
             "complete pair always has a B side that genuinely reordered admission.",
             "# " + rep["note"]]
    for cond in AB.CONDITIONS:
        c = rep["conditions"][cond]
        lines.append("# %s: %d goals, %d settled, success rate %s, turns %s, wall_s %s, "
                     "tool_calls %s, retries %s" % (
                         cond, c["goals"], c["settled"], AB._f(c["success_rate"]),
                         AB._f(c["metrics"]["turns"]["mean"]), AB._f(c["metrics"]["wall_s"]["mean"]),
                         AB._f(c["metrics"]["tool_calls"]["mean"]),
                         AB._f(c["metrics"]["retries"]["mean"])))
    d = rep["mean_paired_delta_B_minus_A"]
    lines.append("# mean paired delta B-A: " + ", ".join(
        "%s %s (n=%d)" % (m, AB._f(d[m]["mean"]), d[m]["n"]) for m in METRICS))
    if rep["incomplete_goals"]:
        lines.append("# incomplete goals: %d (%s)" % (len(rep["incomplete_goals"]), "; ".join(
            "%s: %s" % (g["goal_id"], g["reason"]) for g in rep["incomplete_goals"][:5])))
    for p in rep.get("problems", []):
        lines.append("# manifest problem: " + p)
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Read-only paired comparison for load-aware routing (on vs queue order).")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--fleet-dir", default=None,
                    help="directory holding the fleet records (default: the live one, read-only)")
    ap.add_argument("--min-pairs", type=int, default=DEFAULT_MIN_PAIRS)
    ap.add_argument("--format", choices=("both", "json", "text"), default="both")
    args = ap.parse_args(argv)
    fleet_dir = args.fleet_dir or os.path.join(REPO, ".fleet")
    entries, problems = AB.load_manifest(args.manifest)
    events, workers, camps, _n = S.load_fleet(fleet_dir)
    action = router_action_by_run(fleet_dir)
    rep = compare(entries, events, workers, camps, action, min_pairs=args.min_pairs)
    rep["problems"] = problems
    out = []
    if args.format in ("both", "json"):
        out.append(json.dumps(rep, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    if args.format in ("both", "text"):
        out.append(render_text(rep))
    text = "".join(out)
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.write(text.encode("ascii", "replace").decode("ascii"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
