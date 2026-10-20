# -*- coding: utf-8 -*-
"""Read-only capability profile per ROLE, derived from the fleet's whole recorded history.

WHAT DECISION THIS SERVES. Which kinds of fleet work look reliable and cheap enough to be handed
a kind of task, once enough runs exist to say so. This is slice C1, derivation only: the script
reads and measures. It writes nothing (not into .fleet, not anywhere), is not imported by any
runtime module, and changes no existing behaviour.

THE CONSUMER (documented here so the output is not another file nobody reads). Roadmap item C3,
load-aware routing in SHADOW mode (relay/load_routing.py, setting `fanout_load_routing`), reads
the `profiles` object (role -> labels) through `load_profiles` below, next to the existing
admission margins (maxtabs, rate ceiling, RAM), and records the assignment it WOULD have made
without acting on it. This script itself still writes nothing. A role with `confidence` other
than "unknown" (n_runs >= --min-n) is what makes a profile usable. Nothing writes a profile file
in .fleet, and nothing ever will: the profile is derived again from the records.

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

BY ROLE AND PROTOCOL VERSION (slices D1 and D2). When `org_versions.jsonl` exists in the fleet
directory (written by relay/org/versions.py when the setting `org_version_record` is on), every
profile whose records carry a run id also gets `by_org_version`: the same success rate and mean
turns, split by the content hash of the role and protocol definitions the run started under.
Only records from status.json and history.json carry a run id, so campaign-ledger children and
merges are not split and stay in the group total. A run with no line in the file (before the
setting was turned on) is counted under "(not recorded)", never guessed. Without the file the
output is exactly what it was. Which wording each hash stands for is in `org_definitions`.

SECOND AXIS: KIND OF WORK (slice D0). With `--by kind` (derive(with_kind=True)) every record
also gets a `kind` from relay/task_kind.py; without it the module is not even imported,
derived from its goal text (a child or aggregator takes the goal of the family it belongs to).
The existing kinds were measured and cannot serve: job types are all `fleet_goal`, the split
verdict is not stored per run, and solo/parent/child/aggregator is the role. `profiles_by_kind`
(role -> kind -> the same entry, same --min-n and confidence rules, no tool mix) and
`kind_coverage` are then in the JSON, and the text prints the (role, kind) table. A goal no rule
recognises is "unknown". Nothing is written; no runtime module imports the kind module.

  python scripts/capability_profile.py [--fleet-dir .fleet] [--min-n 5] [--by role|kind]
                                       [--success-good 0.8] [--turns-good 12]
                                       [--format both|json|text]
"""
from __future__ import annotations

import argparse
import json
import os
import re
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
NO_ORG = "(not recorded)"
NO_ORG_READ = "(definitions unreadable)"
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


def load_org(fleet_dir):
    """({run base id: (roles_hash, workflows_hash)}, {(roles_hash, workflows_hash): definitions},
    unreadable count) from `org_versions.jsonl`. Empty when the file is absent. Never raises."""
    try:
        from relay.org import versions as V
        runs, defs, bad = V.load(os.path.join(fleet_dir, V.FILENAME))
        return {V.run_base(k): v for k, v in runs.items()}, defs, bad
    except Exception:
        return {}, {}, 0


def org_label(pair):
    """Short, stable name for a (roles_hash, workflows_hash) pair."""
    if not pair or not pair[0] or not pair[1]:
        return NO_ORG_READ
    return "roles:%s workflows:%s" % (pair[0][:8], pair[1][:8])


def load_records(fleet_dir):
    """(records, unreadable) : the flat run records with their `kind` of work, read-only.

    The same three sources as `load_profiles` (no tool_events), through `build_records`. The
    kind module is imported here, in this report, and nowhere else (D4's report reads records
    per run id through this instead of importing it)."""
    from relay import task_kind as TK
    camps, bad_ca = _read_jsonl_counted(os.path.join(fleet_dir, "campaigns.jsonl"))
    history, bad_hi = _read_history(os.path.join(fleet_dir, "history.json"))
    workers = S._read_status_workers(os.path.join(fleet_dir, "status.json"))
    unreadable = {k: n for k, n in (("campaigns.jsonl", bad_ca), ("history.json", bad_hi)) if n}
    return build_records(workers, camps, history, {}, TK.task_kind), unreadable


def load_profiles(fleet_dir, min_n=DEFAULT_MIN_N):
    """{role: profile entry} for the runtime reader (relay/load_routing.py), read-only.

    The same derivation as `derive`, but without the two inputs that only feed the tool mix and
    the effort split: tool_events.jsonl is the largest record in the directory and the router
    needs neither. Never raises: any read problem leaves that source empty, so the roles it
    would have described come out `unknown`."""
    try:
        camps, _ = _read_jsonl_counted(os.path.join(fleet_dir, "campaigns.jsonl"))
        history, _ = _read_history(os.path.join(fleet_dir, "history.json"))
        workers = S._read_status_workers(os.path.join(fleet_dir, "status.json"))
        return derive([], workers, camps, min_n=min_n, history=history)["profiles"]
    except Exception:
        return {}


# --------------------------------------------------------------------------- records

def build_records(workers, camp_rows, history, effort_by_run, kind_of=None):
    """One flat list of run records from all sources, each counted once.

    A record has: role, outcome, turn (or None), source, effort (or None), and the ids used for
    tool-call attribution (jid, task_id, run_id, name)."""
    records = []
    kind_of = kind_of or (lambda _goal: None)       # no kind unless the report asks for it
    seen_runs, seen_children, seen_merge_campaigns = set(), set(), set()
    # The goal of a fan-out family, by campaign id: a child or an aggregator has no goal of its
    # own in the ledger, and the kind of work it did is the kind of the goal that was split.
    family_goal = {str(r["campaign_id"]): r.get("goal") for r in camp_rows
                   if r.get("kind") == "campaign" and r.get("campaign_id") is not None}
    for w in workers:
        rec = dict(w)
        rec["source"] = SRC_STATUS
        rec["kind"] = kind_of(w.get("goal") or family_goal.get(str(w.get("campaign_id"))))
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
                        "source": SRC_HISTORY, "kind": kind_of(e.get("goal")),
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
                        "source": SRC_CHILD, "effort": None,
                        "kind": kind_of(family_goal.get(cid))})

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
                        "campaign_id": cid, "source": SRC_MERGE, "effort": None,
                        "kind": kind_of(family_goal.get(cid))})
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
           history=None, effort_by_run=None, with_kind=False, org_by_run=None, org_defs=None):
    kind_of = None
    unknown_kind = "unknown"
    if with_kind:
        # Imported here, not at module top: load_profiles (the runtime reader in
        # relay/load_routing.py) shares this module and must never load the kind module.
        from relay import task_kind as TK
        kind_of, unknown_kind = TK.task_kind, TK.UNKNOWN
    records = build_records(workers, camp_rows, history or [], effort_by_run or {}, kind_of)
    groups = {}

    def grp(name):
        return groups.setdefault(name, {"outcomes": [], "turns": [], "tools": {}, "effort": {},
                                        "org": {}})

    org_labels = {}                     # label -> (roles_hash, workflows_hash), for the report
    sources = {}
    by_kind = {}        # (group, kind) -> same shape as a group, without tools
    for w in records:
        outcome = str(w.get("outcome") or "").upper()
        g = grp(group_of(w))
        sources[w["source"]] = sources.get(w["source"], 0) + 1
        if outcome:
            targets = [g]
            if with_kind:
                targets.append(by_kind.setdefault(
                    (group_of(w), w.get("kind") or unknown_kind),
                    {"outcomes": [], "turns": [], "tools": {}, "effort": {}}))
            has_turn = _is_num(w.get("turn"))
            for tg in targets:
                tg["outcomes"].append(outcome)
                if has_turn:
                    tg["turns"].append(w["turn"])
                if w["source"] in (SRC_STATUS, SRC_HISTORY):   # the ledger rows have no policy
                    e = tg["effort"].setdefault(w.get("effort") or NO_EFFORT,
                                                {"outcomes": [], "turns": []})
                    e["outcomes"].append(outcome)
                    if has_turn:
                        e["turns"].append(w["turn"])
                    if org_by_run and tg is g:
                        base = re.sub(r"_a\d+$", "", str(w.get("run_id") or ""))
                        pair = org_by_run.get(base) if base else None
                        label = org_label(pair) if pair else NO_ORG
                        if pair:
                            org_labels[label] = pair
                        o = g["org"].setdefault(label, {"outcomes": [], "turns": []})
                        o["outcomes"].append(outcome)
                        if has_turn:
                            o["turns"].append(w["turn"])

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

    def entry_of(g, with_tools):
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
            "confidence": confidence(n, min_n),
        }
        if with_tools:
            entry["tool_calls"] = sum(g["tools"].values())
            entry["tool_mix"] = _tool_mix(g["tools"])
        if g["effort"]:
            entry["by_effort_reached"] = {
                lvl: {"n": len(e["outcomes"]), "success_rate": _success_rate(e["outcomes"]),
                      "mean_turns": (sum(e["turns"]) / len(e["turns"])) if e["turns"] else None}
                for lvl, e in sorted(g["effort"].items())}
        if any(lbl != NO_ORG for lbl in g.get("org", {})):
            entry["by_org_version"] = {
                lbl: {"n": len(o["outcomes"]), "success_rate": _success_rate(o["outcomes"]),
                      "mean_turns": (sum(o["turns"]) / len(o["turns"])) if o["turns"] else None}
                for lbl, o in sorted(g["org"].items())}
        return entry

    out = {name: entry_of(groups[name], True) for name in sorted(groups)}
    # Second axis (slice D0): the same entry per (role, kind of work). Tool calls are not split
    # by kind: a call row carries no goal, so attributing it to a kind would be a guess.
    kinds = {}
    for (name, kind) in sorted(by_kind):
        kinds.setdefault(name, {})[kind] = entry_of(by_kind[(name, kind)], False)
    coverage = {}
    for (name, kind), g in by_kind.items():
        coverage[kind] = coverage.get(kind, 0) + len(g["outcomes"])
    extra = {"profiles_by_kind": kinds,
             "kind_coverage": dict(sorted(coverage.items()))} if with_kind else {}
    result = {**extra,
            "note": NOTE,
            "identity": "role (producer / subtask / aggregator); an explicit profile field wins",
            "thresholds": {"min_n": min_n, "success_good": success_good,
                           "turns_good": turns_good},
            "sources": dict(sorted(sources.items())),
            "profiles": out, "unattributed_tool_calls": unattributed}
    rep_org = {lbl: org_defs[pair] for lbl, pair in sorted(org_labels.items())
               if pair in (org_defs or {})}
    if rep_org:
        result["org_definitions"] = rep_org
    return result


# --------------------------------------------------------------------------- output

def _f(v, nd=2):
    return "n/a" if v is None else ("%.*f" % (nd, v))


def render_text(rep, n_transcripts=0, by="role"):
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
            for lbl, e in p.get("by_org_version", {}).items():
                lines.append("    org %-44s runs %5d success %s turns %s"
                             % (lbl, e["n"], _f(e["success_rate"]), _f(e["mean_turns"], 1)))
    if by == "kind":
        lines.append("# kind of work (relay/task_kind.py, derived from the goal text); "
                     "coverage: " + ", ".join(
                         "%s %d" % (k, v) for k, v in rep.get("kind_coverage", {}).items()))
        lines.append("%-14s %-12s %6s %8s %-8s %7s %-8s %-10s"
                     % ("role", "kind", "runs", "success", "", "turns", "", "confidence"))
        for role, kinds in rep.get("profiles_by_kind", {}).items():
            for kind, p in kinds.items():
                lines.append("%-14s %-12s %6d %8s %-8s %7s %-8s %-10s" % (
                    role[:14], kind, p["n_runs"], _f(p["success"]["rate"]),
                    p["success"]["label"], _f(p["turns"]["mean"], 1), p["turns"]["label"],
                    p["confidence"]))
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
    ap.add_argument("--by", choices=("role", "kind"), default="role",
                    help="kind: also print the profile per (role, kind of work)")
    args = ap.parse_args(argv)
    fleet_dir = args.fleet_dir or os.path.join(REPO, ".fleet")
    events, workers, camps, n_tr, history, effort, unreadable = load_all(fleet_dir)
    org_runs, org_defs, org_bad = load_org(fleet_dir)
    if org_bad:
        unreadable["org_versions.jsonl"] = org_bad
    rep = derive(events, workers, camps, min_n=args.min_n, success_good=args.success_good,
                 turns_good=args.turns_good, history=history, effort_by_run=effort,
                 with_kind=args.by == "kind", org_by_run=org_runs, org_defs=org_defs)
    if unreadable:
        rep["unreadable"] = unreadable
    out = []
    if args.format in ("both", "json"):
        out.append(json.dumps(rep, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    if args.format in ("both", "text"):
        out.append(render_text(rep, n_tr, by=args.by))
    text = "".join(out)
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.write(text.encode("ascii", "replace").decode("ascii"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
