# -*- coding: utf-8 -*-
"""Read-only capability profile per ROLE, derived from the fleet's whole recorded history.

WHAT DECISION THIS SERVES. Which kinds of fleet work look reliable and cheap enough to be handed
a kind of task, once enough runs exist to say so. This is slice C1, derivation only: the script
reads and measures. It writes nothing (not into .fleet, not anywhere), is not imported by any
runtime module, and changes no existing behaviour.

THE CONSUMER (documented here so the output is not another file nobody reads). Roadmap item C3,
load-aware routing in SHADOW mode, is the intended reader: it would take the JSON `profiles`
object printed by this script (role -> labels) next to the existing admission margins (maxtabs,
rate ceiling, RAM) and record the assignment it WOULD have made, without acting on it. C3 is not
implemented: at the time of writing no code and no settings key for it exist. Until it does, the
only reader is the scheduled roadmap loop, which must run this script over the real .fleet and
look for a role with `confidence` other than "unknown" (n_runs >= --min-n). It must not wait for
a profile file in .fleet: nothing writes one, and nothing ever will.

WHY THE OLD INPUT NEVER FILLED. The first version read only status.json `workers`, which holds
the LAST run only, and named a group after the worker's `profile`, else its `name`. No worker
row carries a `profile`; `name` is a queue slot (w0, w1, ...) that is reused by every run. So
every profile had runs=1 and the group names were slot numbers, not identities. History was
never read although it exists (measured 2026-10-20 on the live .fleet: history.json 458 runs,
campaigns.jsonl 1119 child results, mechanisms.jsonl 9374 effort_policy rows).

IDENTITY: THE ROLE. Candidates the investigation named were effort level, role and split kind.
Measured on the live records:
  role         Recorded for every record once the three sources are read together. history.json
               holds top-level (producer) runs only: no child task id and no parent task id of
               any campaign appears among its jids (0 of 1090 and 0 of 313). The campaign ledger
               holds the children (`child_result`, role subtask) and the merges (`merge_done` /
               `merge_abandoned`, role aggregator). status.json rows and tool_events rows carry
               `role` themselves. So the role of a record is known from where it was recorded.
  effort level The configured level is "auto" for 458 of 458 history runs (no variation, so it
               cannot tell anything apart). The level a run REACHED (effort_policy rows, last
               `level`) differs per run, but the policy runs in shadow mode and escalates after
               trouble, so it labels how hard the run turned out to be, not a setting that was
               chosen. It is therefore printed per role as `by_effort_reached` (descriptive), and
               is not part of the identity.
  split kind   status.json workers carry `fanout.kind`, but history.json drops it and status.json
               keeps the last run only; mechanisms `fanout` rows carry only campaign_id and
               children. Not recorded per run, so it cannot be an identity.
A worker row that does carry an explicit `profile` / `agent_profile` field still wins (a later
version may record one); otherwise the group is the row's `role`, else "(unnamed)". The slot
name is never a group.

INPUTS (all read-only, one --fleet-dir, default the live .fleet):
  status.json         workers (the latest run; role, outcome, turn, ids).
  history.json        one entry per finished top-level run: outcome, turn, run_id, name, jid.
                      Treated as role "producer". Entries already present in status.json
                      (same run_id and name) are counted once, from status.json.
  campaigns.jsonl     `child_result` rows (role subtask; outcome DONE/MISSING; no turn count) and
                      `merge_done` / `merge_abandoned` rows (role aggregator, one verdict per
                      campaign; `merge_requeued` is a retry, not a verdict). A child is counted
                      once per (campaign_id, subtask_index), the last row winning.
  mechanisms.jsonl    `effort_policy` rows, only to learn the effort level a producer run reached.
  tool_events.jsonl   call rows, for the tool mix.
  transcripts/        counted only.
A line that is not valid JSON, or a history entry that is not an object, is skipped and counted
in `unreadable` (per file) so a damaged record is visible instead of silently thinning a group.

PER GROUP
  n_runs      records that carry a non-empty outcome (a worker still running is not a run).
  success     share of those runs whose outcome is DONE/FANOUT. INTERRUPTED (an operator stop) is
              counted as not successful; `outcomes` shows the split so a reader can judge.
  turns       mean of the numeric `turn` over runs that have one (missing is not 0). Subtasks and
              aggregators from the campaign ledger have no turn count, so their turns stay unknown.
  tool_mix    share of the group's attributed tool calls by tool name (gateway discovery
              `call_tool.*` excluded). A call is attributed through the profile or role written on
              the call row, or through its task id / run id as the coordinator wrote them. The slot
              name alone is never used. Unattributable calls are counted, never guessed.

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
ROLE_PRODUCER = "producer"
ROLE_SUBTASK = S.CHILD_ROLE
ROLE_AGGREGATOR = S.MERGE_ROLE
NO_EFFORT = "(none recorded)"
SRC_STATUS = "status.json workers"
SRC_HISTORY = "history.json"
SRC_CHILD = "campaigns.jsonl child_result"
SRC_MERGE = "campaigns.jsonl merge rows"
NOTE = ("Derived from recorded history. 'unknown' means too few runs to judge, not that the "
        "role is bad. Nothing here is wired into scheduling.")


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def group_of(w):
    """Identity of a record: an explicit profile, else its role. Never the slot name."""
    for key in ("profile", "agent_profile", "role"):
        v = w.get(key)
        if v:
            return str(v).lower() if key == "role" else str(v)
    return UNNAMED


# --------------------------------------------------------------------------- reading

def _read_jsonl_counted(path):
    """(rows, unreadable_line_count). A missing file is empty, not an error."""
    rows, bad = [], 0
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
                else:
                    bad += 1
    except OSError:
        pass
    return rows, bad


def _read_history(path):
    """(entries, unreadable_count). The file is one JSON list; a whole-file parse failure counts
    as one unreadable item, a non-object element as one each."""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            data = json.load(fh)
    except OSError:
        return [], 0
    except ValueError:
        return [], 1
    if not isinstance(data, list):
        return [], 1
    entries = [e for e in data if isinstance(e, dict)]
    return entries, len(data) - len(entries)


def effort_reached_by_run(mech_rows):
    """{(run_id, instance): last effort level the policy reported for that run}."""
    out = {}
    for r in mech_rows:
        if r.get("mechanism") != "effort_policy":
            continue
        extra = r.get("extra")
        if not isinstance(extra, dict) or extra.get("event") != "turn":
            continue
        level = extra.get("level")
        if level and r.get("run_id"):
            out[(str(r["run_id"]), str(r.get("instance") or ""))] = str(level)
    return out


def load_all(fleet_dir):
    """Everything the profile reads, each file once, read-only.

    (events, workers, camp_rows, n_transcripts, history, effort_by_run, unreadable)
    where `unreadable` maps file name -> number of skipped records (only non-zero ones)."""
    events, bad_ev = _read_jsonl_counted(os.path.join(fleet_dir, "tool_events.jsonl"))
    camps, bad_ca = _read_jsonl_counted(os.path.join(fleet_dir, "campaigns.jsonl"))
    mech, bad_me = _read_jsonl_counted(os.path.join(fleet_dir, "mechanisms.jsonl"))
    history, bad_hi = _read_history(os.path.join(fleet_dir, "history.json"))
    workers = S._read_status_workers(os.path.join(fleet_dir, "status.json"))
    try:
        n_tr = len(os.listdir(os.path.join(fleet_dir, "transcripts")))
    except OSError:
        n_tr = 0
    unreadable = {name: n for name, n in (("tool_events.jsonl", bad_ev),
                                          ("campaigns.jsonl", bad_ca),
                                          ("mechanisms.jsonl", bad_me),
                                          ("history.json", bad_hi)) if n}
    return events, workers, camps, n_tr, history, effort_reached_by_run(mech), unreadable


# --------------------------------------------------------------------------- records

def build_records(workers, camp_rows, history, effort_by_run):
    """One flat list of run records from all sources, each counted once.

    A record has: role, outcome, turn (or None), source, effort (or None), and the ids used for
    tool-call attribution (jid, task_id, run_id, name)."""
    records = []
    seen_runs, seen_children, seen_merge_campaigns = set(), set(), set()
    for w in workers:
        rec = dict(w)
        rec["source"] = SRC_STATUS
        rec["effort"] = effort_by_run.get((str(w.get("run_id") or ""), str(w.get("name") or ""))) \
            if w.get("run_id") else None
        records.append(rec)
        if w.get("run_id") and w.get("name"):
            seen_runs.add((str(w["run_id"]), str(w["name"])))
        for key in (w.get("task_id"), w.get("jid")):
            if key:
                seen_children.add(str(key))
        if str(w.get("role") or "").lower() == ROLE_AGGREGATOR and w.get("campaign_id"):
            seen_merge_campaigns.add(str(w["campaign_id"]))

    for e in history:
        run, name = str(e.get("run_id") or ""), str(e.get("name") or "")
        if run and name and (run, name) in seen_runs:
            continue
        records.append({"role": ROLE_PRODUCER, "outcome": e.get("outcome"), "turn": e.get("turn"),
                        "run_id": run, "name": name, "jid": e.get("jid"),
                        "source": SRC_HISTORY,
                        "effort": effort_by_run.get((run, name)) if run else None})

    children = {}
    for r in camp_rows:
        if r.get("kind") == "child_result" and r.get("campaign_id") is not None:
            children[(str(r["campaign_id"]), str(r.get("subtask_index")))] = r   # last wins
    for (cid, _idx), r in children.items():
        if r.get("task_id") and str(r["task_id"]) in seen_children:
            continue        # status.json knows this child; it wins
        records.append({"role": ROLE_SUBTASK, "outcome": r.get("outcome"), "turn": None,
                        "task_id": r.get("task_id"), "campaign_id": cid,
                        "source": SRC_CHILD, "effort": None})

    merges = {}
    for r in camp_rows:
        kind = r.get("kind")
        if kind in ("merge_done", "merge_abandoned") and r.get("campaign_id") is not None:
            cid = str(r["campaign_id"])
            if kind == "merge_done" or merges.get(cid) != "DONE":
                merges[cid] = "DONE" if kind == "merge_done" else "ABANDONED"
    for cid, outcome in merges.items():
        if cid in seen_merge_campaigns:
            continue
        records.append({"role": ROLE_AGGREGATOR, "outcome": outcome, "turn": None,
                        "campaign_id": cid, "source": SRC_MERGE, "effort": None})
    return records


# --------------------------------------------------------------------------- derivation

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


def _attribution_maps(records):
    """task / (run, name) -> group. The slot name alone is not an identity (it is reused by every
    run), so there is no name-only fallback."""
    by_task, by_run_name = {}, {}
    for w in records:
        g = group_of(w)
        for key in (w.get("jid"), w.get("task_id")):
            if key:
                by_task[str(key)] = g
        if w.get("run_id") and w.get("name"):
            by_run_name[(str(w["run_id"]), str(w["name"]))] = g
    return by_task, by_run_name


def _success_rate(outcomes):
    n = len(outcomes)
    return (sum(1 for o in outcomes if o in S.TERMINAL_OK) / n) if n else None


def derive(events, workers, camp_rows, min_n=DEFAULT_MIN_N,
           success_good=DEFAULT_SUCCESS_GOOD, turns_good=DEFAULT_TURNS_GOOD,
           history=None, effort_by_run=None):
    records = build_records(workers, camp_rows, history or [], effort_by_run or {})
    groups = {}

    def grp(name):
        return groups.setdefault(name, {"outcomes": [], "turns": [], "tools": {}, "effort": {}})

    sources = {}
    for w in records:
        outcome = str(w.get("outcome") or "").upper()
        g = grp(group_of(w))
        sources[w["source"]] = sources.get(w["source"], 0) + 1
        if outcome:
            g["outcomes"].append(outcome)
            has_turn = _is_num(w.get("turn"))
            if has_turn:
                g["turns"].append(w["turn"])
            if w["source"] in (SRC_STATUS, SRC_HISTORY):   # the ledger rows have no policy
                e = g["effort"].setdefault(w.get("effort") or NO_EFFORT,
                                           {"outcomes": [], "turns": []})
                e["outcomes"].append(outcome)
                if has_turn:
                    e["turns"].append(w["turn"])

    by_task, by_run_name = _attribution_maps(records)
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
        elif call.get("role"):
            name = str(call["role"]).lower()
        elif sub and str(sub) in by_task:
            name = by_task[str(sub)]
        elif task and str(task) in by_task:
            name = by_task[str(task)]
        elif task and (str(task), str(worker or "")) in by_run_name:
            name = by_run_name[(str(task), str(worker))]
        if name is None:
            unattributed += 1
            continue
        t = grp(name)["tools"]
        t[tool] = t.get(tool, 0) + 1

    out = {}
    for name in sorted(groups):
        g = groups[name]
        n = len(g["outcomes"])
        rate = _success_rate(g["outcomes"])
        nt = len(g["turns"])
        mean_turns = (sum(g["turns"]) / nt) if nt else None
        counts = {}
        for o in g["outcomes"]:
            counts[o] = counts.get(o, 0) + 1
        entry = {
            "n_runs": n,
            "success": {"rate": rate, "n": n, "label": label_rate(rate, n, min_n, success_good)},
            "turns": {"mean": mean_turns, "n": nt,
                      "label": label_cost(mean_turns, nt, min_n, turns_good)},
            "outcomes": dict(sorted(counts.items())),
            "tool_calls": sum(g["tools"].values()),
            "tool_mix": _tool_mix(g["tools"]),
            "confidence": confidence(n, min_n),
        }
        if g["effort"]:
            entry["by_effort_reached"] = {
                lvl: {"n": len(e["outcomes"]), "success_rate": _success_rate(e["outcomes"]),
                      "mean_turns": (sum(e["turns"]) / len(e["turns"])) if e["turns"] else None}
                for lvl, e in sorted(g["effort"].items())}
        out[name] = entry
    return {"note": NOTE,
            "identity": "role (producer / subtask / aggregator); an explicit profile field wins",
            "thresholds": {"min_n": min_n, "success_good": success_good,
                           "turns_good": turns_good},
            "sources": dict(sorted(sources.items())),
            "profiles": out, "unattributed_tool_calls": unattributed}


# --------------------------------------------------------------------------- output

def _f(v, nd=2):
    return "n/a" if v is None else ("%.*f" % (nd, v))


def render_text(rep, n_transcripts=0):
    t = rep["thresholds"]
    lines = ["# Capability profile (min n %d, success good >= %s, turns good <= %s)"
             % (t["min_n"], _f(t["success_good"]), _f(t["turns_good"])), "# " + rep["note"],
             "# identity: " + rep.get("identity", "")]
    if rep.get("sources"):
        lines.append("# records read: " + ", ".join(
            "%s %d" % (k, v) for k, v in rep["sources"].items()))
    if rep.get("unreadable"):
        lines.append("# SKIPPED UNREADABLE RECORDS: " + ", ".join(
            "%s %d" % (k, v) for k, v in sorted(rep["unreadable"].items())))
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
            for lvl, e in p.get("by_effort_reached", {}).items():
                lines.append("    effort reached %-16s runs %5d success %s turns %s"
                             % (lvl, e["n"], _f(e["success_rate"]), _f(e["mean_turns"], 1)))
    lines.append("# unattributed tool calls: %d; transcript files: %d"
                 % (rep["unattributed_tool_calls"], n_transcripts))
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only capability profile from fleet history.")
    ap.add_argument("--fleet-dir", default=None,
                    help="directory holding the fleet records (default: the live one, read-only)")
    ap.add_argument("--min-n", type=int, default=DEFAULT_MIN_N)
    ap.add_argument("--success-good", type=float, default=DEFAULT_SUCCESS_GOOD)
    ap.add_argument("--turns-good", type=float, default=DEFAULT_TURNS_GOOD)
    ap.add_argument("--format", choices=("both", "json", "text"), default="both")
    args = ap.parse_args(argv)
    fleet_dir = args.fleet_dir or os.path.join(REPO, ".fleet")
    events, workers, camps, n_tr, history, effort, unreadable = load_all(fleet_dir)
    rep = derive(events, workers, camps, min_n=args.min_n, success_good=args.success_good,
                 turns_good=args.turns_good, history=history, effort_by_run=effort)
    if unreadable:
        rep["unreadable"] = unreadable
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
