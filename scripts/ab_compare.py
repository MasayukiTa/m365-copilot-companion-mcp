# -*- coding: utf-8 -*-
"""Read-only A/B comparison of two conditions over the same small goal set, from existing telemetry.

WHAT DECISION THIS SERVES. Whether a switch (for example fan-out on vs off) changed outcome or
cost on the SAME goals. This script only reads and measures. It schedules nothing, submits
nothing and writes nothing; running the two conditions is the operator's job.

MANIFEST (JSON array, {"entries": [...]}, or JSONL). One entry per (goal, condition):
    {"goal_id": "g1", "condition": "A", "experiment_id": "exp-20261014-101500-ab12cd",
     "campaign_id": "c84ec492b5b7e"}
`condition` is "A" or "B". `experiment_id` is the id the operator generated for the comparison
(relay.selfimprove.experiment.new_experiment_id makes one); a pair is only compared when both of
its sides carry the SAME experiment id. `campaign_id` names the campaign the goal ran as (ledger
`campaign_id`); `run_id` may be given instead for a run that appears only in status.json. An entry
that is malformed, or that repeats a (goal, condition), is reported under `problems` and takes its
goal out of the comparison; it is never silently picked.

INPUTS (all read-only, one --fleet-dir, default the live .fleet; same files and readers as
scripts/sibling_dup_report.py): campaigns.jsonl, status.json, tool_events.jsonl.

PER RECORD
  settled    every expected child has an outcome and a started merge has one. Unsettled records
             and unknown campaigns are excluded from rates and pairs, and counted.
  success    settled AND all children are DONE/FANOUT AND (no merge OR a merge finished OK).
  turns      sum of `turn` over this campaign's workers in status.json. status.json keeps only the
             last run's workers, so a finished campaign usually has none: turns is then None
             (missing), never 0.
  wall_s     span from the earliest to the latest timestamp among the campaign's attributed tool
             calls/outcomes and its workers' phase events. A proxy, None when there is no timestamp.
  tool_calls attributed sibling calls (gateway discovery `call_tool.*` excluded); None when none.
  retries    ledger child_requeued + merge_requeued rows plus workers' fresh_replay_count.

PAIRING. A goal is a complete pair when both conditions are settled and share an experiment id.
Deltas are B minus A, computed per goal and only for metrics present on both sides; the mean of
the deltas states how many goals it used.

VERDICT. INSUFFICIENT when complete pairs < --min-pairs (default 5). Otherwise DIRECTION_ONLY.
There is no significance test on purpose: with a handful of goals the result tells which way the
numbers leaned, not that the difference is real. The text says so every time.

  python scripts/ab_compare.py --manifest ab.json [--fleet-dir .fleet] [--min-pairs 5]
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

from scripts import sibling_dup_report as S  # noqa: E402

DEFAULT_MIN_PAIRS = 5
CONDITIONS = ("A", "B")
METRICS = ("success", "turns", "wall_s", "tool_calls", "retries")
DIRECTION_ONLY_NOTE = ("Direction-only: with this few goals the numbers show which way the "
                       "conditions leaned, not that the difference is real. No significance is "
                       "claimed.")


# --------------------------------------------------------------------------- manifest

def _parse_manifest_text(text):
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                rows.append({"_unparseable": line[:80]})
        return rows
    if isinstance(data, dict):
        data = data.get("entries", [data] if "goal_id" in data else [])
    return data if isinstance(data, list) else []


def load_manifest(path):
    """(entries by (goal, condition), problems). A repeated key poisons the goal, see below."""
    with open(path, "r", encoding="utf-8-sig") as fh:
        raw = _parse_manifest_text(fh.read())
    entries, problems, dup_goals = {}, [], set()
    for i, e in enumerate(raw):
        where = "entry %d" % (i + 1)
        if not isinstance(e, dict) or "_unparseable" in e:
            problems.append("%s: not a JSON object" % where)
            continue
        goal = str(e.get("goal_id") or "").strip()
        cond = str(e.get("condition") or "").strip().upper()
        exp = str(e.get("experiment_id") or "").strip()
        cid = str(e.get("campaign_id") or "").strip()
        rid = str(e.get("run_id") or "").strip()
        if not goal or cond not in CONDITIONS or not exp or not (cid or rid):
            problems.append("%s: needs goal_id, condition A/B, experiment_id and campaign_id "
                            "or run_id" % where)
            continue
        key = (goal, cond)
        if key in entries:
            problems.append("%s: goal %s condition %s given twice; goal excluded"
                            % (where, goal, cond))
            dup_goals.add(goal)
            continue
        entries[key] = {"goal_id": goal, "condition": cond, "experiment_id": exp,
                        "campaign_id": cid, "run_id": rid}
    for goal in dup_goals:
        for cond in CONDITIONS:
            entries.pop((goal, cond), None)
    return entries, problems


# --------------------------------------------------------------------------- measuring

def _ts(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _index(events, workers, camp_rows):
    """Everything per campaign, built once: roster, workers, calls, timestamps, requeues."""
    roster = S.Roster(workers, camp_rows)
    idx = {"roster": roster, "workers": {}, "calls": {}, "times": {}, "requeues": {},
           "expected": {}, "run_workers": {}}
    for w in workers:
        if w.get("campaign_id"):
            idx["workers"].setdefault(str(w["campaign_id"]), []).append(w)
        if w.get("run_id"):
            idx["run_workers"].setdefault(str(w["run_id"]), []).append(w)

    def stamp(cid, t):
        if t is not None:
            idx["times"].setdefault(cid, []).append(t)

    for cid, ws in idx["workers"].items():
        for w in ws:
            for ev in w.get("phase_events") or []:
                if isinstance(ev, dict):
                    stamp(cid, _ts(ev.get("ts")))
    for r in camp_rows:
        cid = str(r.get("campaign_id") or "")
        if not cid:
            continue
        kind = r.get("kind")
        if kind in ("child_requeued", "merge_requeued"):
            idx["requeues"][cid] = idx["requeues"].get(cid, 0) + 1
        elif kind == "campaign" and isinstance(r.get("n"), int):
            idx["expected"][cid] = r["n"]
    for call, outcome in S.pair_events(events):
        tool = str(call.get("tool") or "")
        if tool.startswith(S.DISCOVERY_PREFIX):
            continue
        direct = call.get("campaign_id") and call.get("subtask_id")
        if not direct and (call.get("attr") not in S.ATTRIBUTED_KINDS
                           or not (call.get("task") or call.get("worker"))):
            continue
        hit = roster.resolve_call(call)
        if hit is None:
            continue
        cid = hit[0]
        idx["calls"][cid] = idx["calls"].get(cid, 0) + 1
        stamp(cid, _ts(call.get("ts")))
        if outcome:
            stamp(cid, _ts(outcome.get("ts")))
    return idx


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def measure(entry, idx):
    """One record's measures. `status` is settled / unsettled / unknown_campaign."""
    rec = dict(entry)
    rec.update({"status": "unknown_campaign", "success": None, "turns": None, "wall_s": None,
                "tool_calls": None, "retries": None})
    roster = idx["roster"]
    cid = entry.get("campaign_id") or ""
    if cid:
        kids = roster.children.get(cid, {})
        merges = roster.merges.get(cid, [])
        if not kids and cid not in roster.known:
            return rec
        expected = idx["expected"].get(cid, len(kids))
        merge_pending = (cid in roster.merged_marker and not merges) \
            or any(not o for o in merges)
        settled = (len(kids) >= expected and len(kids) > 0 and all(kids.values())
                   and not merge_pending)
        ws = idx["workers"].get(cid, [])
        rec["status"] = "settled" if settled else "unsettled"
        if settled:
            rec["success"] = int(all(o in S.TERMINAL_OK for o in kids.values())
                                 and (not merges or any(o in S.TERMINAL_OK for o in merges)))
        if ws:
            rec["turns"] = sum(_num(w.get("turn")) for w in ws)
        times = idx["times"].get(cid, [])
        if len(times) >= 2 and max(times) > min(times):
            rec["wall_s"] = max(times) - min(times)
        rec["tool_calls"] = idx["calls"].get(cid) or None
        rec["retries"] = idx["requeues"].get(cid, 0) + sum(
            _num(w.get("fresh_replay_count")) for w in ws)
        return rec
    ws = idx["run_workers"].get(entry.get("run_id") or "", [])
    if not ws:
        return rec
    outcomes = [str(w.get("outcome") or "").upper() for w in ws]
    settled = all(outcomes)
    rec["status"] = "settled" if settled else "unsettled"
    if settled:
        rec["success"] = int(all(o in S.TERMINAL_OK for o in outcomes))
    rec["turns"] = sum(_num(w.get("turn")) for w in ws)
    rec["retries"] = sum(_num(w.get("fresh_replay_count")) for w in ws)
    times = [t for w in ws for ev in (w.get("phase_events") or [])
             if isinstance(ev, dict) for t in [_ts(ev.get("ts"))] if t is not None]
    if len(times) >= 2 and max(times) > min(times):
        rec["wall_s"] = max(times) - min(times)
    return rec


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def compare(entries, events, workers, camp_rows, min_pairs=DEFAULT_MIN_PAIRS):
    idx = _index(events, workers, camp_rows)
    recs = {k: measure(e, idx) for k, e in entries.items()}
    conditions = {}
    for cond in CONDITIONS:
        mine = [r for (g, c), r in sorted(recs.items()) if c == cond]
        settled = [r for r in mine if r["status"] == "settled"]
        conditions[cond] = {
            "goals": len(mine), "settled": len(settled),
            "unsettled": sum(1 for r in mine if r["status"] == "unsettled"),
            "unknown_campaign": sum(1 for r in mine if r["status"] == "unknown_campaign"),
            "success_rate": _mean([r["success"] for r in settled]),
            "metrics": {m: {"mean": _mean([r[m] for r in settled]),
                            "n": sum(1 for r in settled if r[m] is not None)}
                        for m in METRICS[1:]},
        }
    pairs, incomplete = [], []
    for goal in sorted({g for g, _ in recs}):
        a, b = recs.get((goal, "A")), recs.get((goal, "B"))
        if a is None or b is None:
            incomplete.append({"goal_id": goal, "reason": "only one condition in manifest"})
        elif a["status"] != "settled" or b["status"] != "settled":
            incomplete.append({"goal_id": goal, "reason": "A %s, B %s" % (a["status"], b["status"])})
        elif a["experiment_id"] != b["experiment_id"]:
            incomplete.append({"goal_id": goal, "reason": "experiment_id differs between sides"})
        else:
            delta = {m: (b[m] - a[m]) if a[m] is not None and b[m] is not None else None
                     for m in METRICS}
            pairs.append({"goal_id": goal, "experiment_id": a["experiment_id"],
                          "A": {m: a[m] for m in METRICS}, "B": {m: b[m] for m in METRICS},
                          "delta_B_minus_A": delta})
    mean_delta = {m: {"mean": _mean([p["delta_B_minus_A"][m] for p in pairs]),
                      "n": sum(1 for p in pairs if p["delta_B_minus_A"][m] is not None)}
                  for m in METRICS}
    complete = len(pairs)
    verdict = "INSUFFICIENT" if complete < min_pairs else "DIRECTION_ONLY"
    return {
        "verdict": verdict, "complete_pairs": complete, "min_pairs": min_pairs,
        "note": DIRECTION_ONLY_NOTE, "conditions": conditions, "pairs": pairs,
        "mean_paired_delta_B_minus_A": mean_delta, "incomplete_goals": incomplete,
        "experiment_ids": sorted({e["experiment_id"] for e in entries.values()}),
    }


# --------------------------------------------------------------------------- output

def _f(v, nd=2):
    return "n/a" if v is None else ("%.*f" % (nd, v))


def render_text(rep):
    lines = ["# A/B comparison: verdict %s (%d complete pairs, need %d)"
             % (rep["verdict"], rep["complete_pairs"], rep["min_pairs"]),
             "# " + rep["note"]]
    for cond in CONDITIONS:
        c = rep["conditions"][cond]
        lines.append("# %s: %d goals, %d settled, success rate %s, turns %s, wall_s %s, "
                     "tool_calls %s, retries %s" % (
                         cond, c["goals"], c["settled"], _f(c["success_rate"]),
                         _f(c["metrics"]["turns"]["mean"]), _f(c["metrics"]["wall_s"]["mean"]),
                         _f(c["metrics"]["tool_calls"]["mean"]),
                         _f(c["metrics"]["retries"]["mean"])))
    d = rep["mean_paired_delta_B_minus_A"]
    lines.append("# mean paired delta B-A: " + ", ".join(
        "%s %s (n=%d)" % (m, _f(d[m]["mean"]), d[m]["n"]) for m in METRICS))
    if rep["incomplete_goals"]:
        lines.append("# incomplete goals: %d (%s)" % (len(rep["incomplete_goals"]), "; ".join(
            "%s: %s" % (g["goal_id"], g["reason"]) for g in rep["incomplete_goals"][:5])))
    for p in rep.get("problems", []):
        lines.append("# manifest problem: " + p)
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only A/B comparison from fleet telemetry.")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--fleet-dir", default=None,
                    help="directory holding the fleet records (default: the live one, read-only)")
    ap.add_argument("--min-pairs", type=int, default=DEFAULT_MIN_PAIRS)
    ap.add_argument("--format", choices=("both", "json", "text"), default="both")
    args = ap.parse_args(argv)
    fleet_dir = args.fleet_dir or os.path.join(REPO, ".fleet")
    entries, problems = load_manifest(args.manifest)
    events, workers, camps, _n = S.load_fleet(fleet_dir)
    rep = compare(entries, events, workers, camps, min_pairs=args.min_pairs)
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
