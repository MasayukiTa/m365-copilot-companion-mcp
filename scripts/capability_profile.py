# -*- coding: utf-8 -*-
"""Read-only capability profile per worker/profile, derived from existing fleet telemetry.

WHAT DECISION THIS SERVES. Which worker profiles look reliable and cheap enough to be handed a
kind of task, once enough runs exist to say so. This is slice C1, derivation only: the script
reads and measures. It writes nothing (not into .fleet, not anywhere), is not imported by any
runtime module, and changes no existing behaviour. Whether to ever act on a profile is a later,
separate decision.

INPUTS (all read-only, one --fleet-dir, default the live .fleet; same readers as
scripts/sibling_dup_report.py and scripts/ab_compare.py): status.json workers,
campaigns.jsonl, tool_events.jsonl, and the transcripts directory only as a file count.

GROUPING. A worker row belongs to the profile named by its `profile` (or `agent_profile`) field,
else by its `name`. A worker with neither is grouped under "(unnamed)".

PER GROUP
  n_runs      workers that carry a non-empty outcome (a worker still running is not a run).
  success     share of those runs whose outcome is DONE/FANOUT.
  turns       mean of the numeric `turn` over runs that have one (missing is not 0).
  tool_mix    share of the group's attributed tool calls by tool name (gateway discovery
              `call_tool.*` excluded). Calls are attributed through the worker name / profile
              written on the call row, or through its task id / run id as the coordinator wrote
              them. Unattributable calls are counted, never guessed.

THREE-VALUED METRICS. Each of `success` and `turns` is "good", "poor" or "unknown".
  unknown   fewer than --min-n (default 5) runs carry that metric. No rate is judged on less.
  success   good when the success rate >= --success-good (default 0.8), else poor.
  turns     good when the mean turns <= --turns-good (default 12), else poor.
`confidence` is "unknown" (n < min-n), "low" (n < 4 * min-n) or "adequate". The numbers are
still printed when the label is unknown, so a reader can see what was too thin to judge.

  python scripts/capability_profile.py [--fleet-dir .fleet] [--min-n 5]
                                       [--success-good 0.8] [--turns-good 12]
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

DEFAULT_MIN_N = 5
DEFAULT_SUCCESS_GOOD = 0.8
DEFAULT_TURNS_GOOD = 12.0
LOW_CONFIDENCE_FACTOR = 4
UNNAMED = "(unnamed)"
GOOD, POOR, UNKNOWN = "good", "poor", "unknown"
NOTE = ("Derived from sparse telemetry. 'unknown' means too few runs to judge, not that the "
        "profile is bad. Nothing here is wired into scheduling.")


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def group_of(w):
    for key in ("profile", "agent_profile", "name"):
        v = w.get(key)
        if v:
            return str(v)
    return UNNAMED


def _tool_mix(counts):
    total = sum(counts.values())
    return {t: {"calls": c, "share": c / total} for t, c in sorted(counts.items())} if total \
        else {}


def label_rate(value, n, min_n, good_at):
    if n < min_n or value is None:
        return UNKNOWN
    return GOOD if value >= good_at else POOR


def label_cost(value, n, min_n, good_at_most):
    if n < min_n or value is None:
        return UNKNOWN
    return GOOD if value <= good_at_most else POOR


def confidence(n, min_n):
    if n < min_n:
        return UNKNOWN
    return "low" if n < LOW_CONFIDENCE_FACTOR * min_n else "adequate"


def _attribution_maps(workers):
    by_task, by_run_name, by_name = {}, {}, {}
    for w in workers:
        g = group_of(w)
        for key in (w.get("jid"), w.get("task_id")):
            if key:
                by_task[str(key)] = g
        if w.get("run_id") and w.get("name"):
            by_run_name[(str(w["run_id"]), str(w["name"]))] = g
        if w.get("name"):
            by_name.setdefault(str(w["name"]), g)
    return by_task, by_run_name, by_name


def derive(events, workers, camp_rows, min_n=DEFAULT_MIN_N,
           success_good=DEFAULT_SUCCESS_GOOD, turns_good=DEFAULT_TURNS_GOOD):
    groups = {}

    def grp(name):
        return groups.setdefault(name, {"outcomes": [], "turns": [], "tools": {}})

    for w in workers:
        outcome = str(w.get("outcome") or "").upper()
        g = grp(group_of(w))
        if outcome:
            g["outcomes"].append(outcome)
            if _is_num(w.get("turn")):
                g["turns"].append(w["turn"])

    by_task, by_run_name, by_name = _attribution_maps(workers)
    unattributed = 0
    for call, _outcome in S.pair_events(events):
        tool = str(call.get("tool") or "")
        if not tool or tool.startswith(S.DISCOVERY_PREFIX):
            continue
        name = None
        sub = call.get("subtask_id")
        task, worker = call.get("task"), call.get("worker")
        if call.get("profile"):
            name = str(call["profile"])
        elif sub and str(sub) in by_task:
            name = by_task[str(sub)]
        elif task and str(task) in by_task:
            name = by_task[str(task)]
        elif task and (str(task), str(worker or "")) in by_run_name:
            name = by_run_name[(str(task), str(worker))]
        elif worker and str(worker) in by_name:
            name = by_name[str(worker)]
        if name is None:
            unattributed += 1
            continue
        t = grp(name)["tools"]
        t[tool] = t.get(tool, 0) + 1

    out = {}
    for name in sorted(groups):
        g = groups[name]
        n = len(g["outcomes"])
        rate = (sum(1 for o in g["outcomes"] if o in S.TERMINAL_OK) / n) if n else None
        nt = len(g["turns"])
        mean_turns = (sum(g["turns"]) / nt) if nt else None
        out[name] = {
            "n_runs": n,
            "success": {"rate": rate, "n": n, "label": label_rate(rate, n, min_n, success_good)},
            "turns": {"mean": mean_turns, "n": nt,
                      "label": label_cost(mean_turns, nt, min_n, turns_good)},
            "tool_calls": sum(g["tools"].values()),
            "tool_mix": _tool_mix(g["tools"]),
            "confidence": confidence(n, min_n),
        }
    return {"note": NOTE,
            "thresholds": {"min_n": min_n, "success_good": success_good,
                           "turns_good": turns_good},
            "profiles": out, "unattributed_tool_calls": unattributed}


# --------------------------------------------------------------------------- output

def _f(v, nd=2):
    return "n/a" if v is None else ("%.*f" % (nd, v))


def render_text(rep, n_transcripts=0):
    t = rep["thresholds"]
    lines = ["# Capability profile (min n %d, success good >= %s, turns good <= %s)"
             % (t["min_n"], _f(t["success_good"]), _f(t["turns_good"])), "# " + rep["note"]]
    if not rep["profiles"]:
        lines.append("# no worker records found")
    else:
        lines.append("%-24s %6s %8s %-8s %7s %-8s %-10s top tools"
                     % ("profile", "runs", "success", "", "turns", "", "confidence"))
        for name, p in rep["profiles"].items():
            top = sorted(p["tool_mix"].items(), key=lambda kv: (-kv[1]["calls"], kv[0]))[:3]
            lines.append("%-24s %6d %8s %-8s %7s %-8s %-10s %s" % (
                name[:24], p["n_runs"], _f(p["success"]["rate"]), p["success"]["label"],
                _f(p["turns"]["mean"], 1), p["turns"]["label"], p["confidence"],
                ", ".join("%s %.0f%%" % (k, 100 * v["share"]) for k, v in top) or "-"))
    lines.append("# unattributed tool calls: %d; transcript files: %d"
                 % (rep["unattributed_tool_calls"], n_transcripts))
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only capability profile from fleet telemetry.")
    ap.add_argument("--fleet-dir", default=None,
                    help="directory holding the fleet records (default: the live one, read-only)")
    ap.add_argument("--min-n", type=int, default=DEFAULT_MIN_N)
    ap.add_argument("--success-good", type=float, default=DEFAULT_SUCCESS_GOOD)
    ap.add_argument("--turns-good", type=float, default=DEFAULT_TURNS_GOOD)
    ap.add_argument("--format", choices=("both", "json", "text"), default="both")
    args = ap.parse_args(argv)
    fleet_dir = args.fleet_dir or os.path.join(REPO, ".fleet")
    events, workers, camps, n_tr = S.load_fleet(fleet_dir)
    rep = derive(events, workers, camps, min_n=args.min_n, success_good=args.success_good,
                 turns_good=args.turns_good)
    out = []
    if args.format in ("both", "json"):
        out.append(json.dumps(rep, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    if args.format in ("both", "text"):
        out.append(render_text(rep, n_tr))
    text = "".join(out)
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.write(text.encode("ascii", "replace").decode("ascii"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
