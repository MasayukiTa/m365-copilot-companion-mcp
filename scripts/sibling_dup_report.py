# -*- coding: utf-8 -*-
"""Read-only report: how much work fan-out siblings duplicate, and what the merge loses.

WHAT DECISION THIS SERVES. Whether siblings of one fan-out campaign repeat each other's tool
calls often enough, and whether the merge drops enough child information, to justify building
anything that shares work between siblings. This script only measures; it changes nothing.

INPUTS (all read-only, all under one --fleet-dir, default the live .fleet):
  tool_events.jsonl  call/outcome rows written by tools/tool_ledger.py (tool, args digests, ts,
                     mono, task, worker, attr). task/worker are filled only under the
                     attribution kinds in ATTRIBUTED_KINDS.
  status.json        `workers`: campaign_id, role, outcome, jid, task_id, run_id, name.
  campaigns.jsonl    campaign headers, one row per subtask (task_id), and `merged` markers.
  transcripts/       counted only; outcomes come from status.json, not from transcripts.

MEASURES
  M1  sibling duplicate-call rate: calls whose tool+argument digests were already issued by a
      DIFFERENT sibling of the same campaign (first issuer is not a duplicate) / all sibling
      calls that carry a comparable key.
  M2  time on duplicated calls (sum of their durations) as a share of campaign wall-clock.
      Wall-clock PROXY: span from the first attributed call to the last outcome of the campaign.
      Siblings run in parallel, so summed duplicate time can exceed the span; that is work, not
      elapsed time, and it is reported as measured.
  M3  merge loss. The data carries no text of a merge's inputs, so the proxy is structural:
      a campaign with a merge worker (role aggregator) whose merge did not finish OK, or that
      ran while >= 1 child slice was not DONE, counts as a merge failure; it is "attributable to
      missing child information" when slices were missing at merge time.
  M4  denominator: campaigns with >= 2 completed children AND >= 1 attributed sibling call.

HONESTY RULES. A row whose attribution is empty/ambiguous, or whose task cannot be mapped to a
campaign child, goes into a named bucket and is never counted as a duplicate or as a success.
A call without argument digests cannot be compared and is excluded from the M1 denominator
(counted). A call without an outcome has no duration and is counted, never averaged as zero.
Gateway discovery chatter (tool names starting with `call_tool.`) is excluded: every sibling
issues the same catalogue lookups, which would read as duplication without being work.

  python scripts/sibling_dup_report.py [--fleet-dir .fleet] [--top 50]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Attribution kinds under which tool_events carry a trustworthy task/worker. `ambiguous` and
#: missing attribution are deliberately absent: they go to the unknown bucket.
ATTRIBUTED_KINDS = ("explicit", "session", "window", "session-window")

#: Gateway-internal lookups, excluded from duplication (see HONESTY RULES).
DISCOVERY_PREFIX = "call_tool."

#: Verdict thresholds. Stated here, not buried in the logic.
MIN_CAMPAIGNS = 20                  # fewer qualifying campaigns -> INSUFFICIENT
DUP_TIME_SHARE_LIMIT = 0.05         # duplicate time must be < 5% of campaign wall-clock ...
MERGE_LOSS_LIMIT = 2.0 / 10.0       # ... AND < 2 of 10 merges lost child information.

TERMINAL_OK = ("DONE", "FANOUT")
CHILD_ROLE = "subtask"
MERGE_ROLE = "aggregator"


def _read_jsonl(path):
    rows = []
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError:
        pass
    return rows


def _read_status_workers(path):
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    workers = data.get("workers") if isinstance(data, dict) else None
    return [w for w in (workers or []) if isinstance(w, dict)]


def load_fleet(fleet_dir):
    """(event rows, status workers, campaign rows, transcript file count). Read-only."""
    events = _read_jsonl(os.path.join(fleet_dir, "tool_events.jsonl"))
    workers = _read_status_workers(os.path.join(fleet_dir, "status.json"))
    camps = _read_jsonl(os.path.join(fleet_dir, "campaigns.jsonl"))
    tdir = os.path.join(fleet_dir, "transcripts")
    try:
        n_tr = len(os.listdir(tdir))
    except OSError:
        n_tr = 0
    return events, workers, camps, n_tr


def pair_events(rows):
    """[(call_row, outcome_row_or_None)] in call order."""
    outcomes = {r.get("id"): r for r in rows if r.get("event") == "outcome"}
    return [(r, outcomes.get(r.get("id"))) for r in rows if r.get("event") == "call"]


def _duration(outcome):
    if not outcome:
        return None
    for key in ("dur_mono_s", "duration_s"):
        v = outcome.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            return float(v)
    return None


def _arg_key(call):
    """Comparable identity of a call: tool + argument digests, or None when not comparable."""
    args = call.get("args")
    tool = str(call.get("tool") or "")
    if not tool or not isinstance(args, dict):
        return None
    parts = []
    for name in sorted(args):
        v = args[name]
        if isinstance(v, dict):
            d = v.get("sha16")
            if d is None:
                d = "redacted" if v.get("redacted") else json.dumps(v, sort_keys=True,
                                                                    default=str)
        else:
            d = json.dumps(v, sort_keys=True, default=str)
        parts.append("%s=%s" % (name, d))
    return tool + "|" + "&".join(parts)


class Roster:
    """Maps a ledger row's (task, worker) to (campaign_id, child_id), or None."""

    def __init__(self, workers, camp_rows):
        self.child_of = {}          # task string -> (campaign, child_id)
        self.by_run_name = {}       # (run_id, name) -> (campaign, child_id)
        self.by_run = {}            # run_id -> set of (campaign, child_id)
        self.children = {}          # campaign -> {child_id: outcome}
        self.merges = {}            # campaign -> [merge outcome]
        self.known = set()
        for w in workers:
            cid = w.get("campaign_id") or ""
            role = str(w.get("role") or "").lower()
            outcome = str(w.get("outcome") or "").upper()
            if not cid:
                continue
            self.known.add(cid)
            if role == MERGE_ROLE:
                self.merges.setdefault(cid, []).append(outcome)
                continue
            if role != CHILD_ROLE:
                continue
            child = w.get("task_id") or w.get("jid") or ("%s/%s" % (w.get("run_id"),
                                                                    w.get("name")))
            self.children.setdefault(cid, {})[child] = outcome
            ident = (cid, child)
            for key in (w.get("jid"), w.get("task_id")):
                if key:
                    self.child_of[str(key)] = ident
            if w.get("run_id"):
                self.by_run_name[(str(w["run_id"]), str(w.get("name") or ""))] = ident
                self.by_run.setdefault(str(w["run_id"]), set()).add(ident)
        self.merged_marker = set()
        for r in camp_rows:
            cid = r.get("campaign_id")
            if not cid:
                continue
            self.known.add(cid)
            if r.get("kind") == "merged":
                self.merged_marker.add(cid)
            elif r.get("task_id"):
                self.child_of.setdefault(str(r["task_id"]), (cid, str(r["task_id"])))

    def resolve(self, task, worker):
        if task:
            hit = self.child_of.get(str(task))
            if hit:
                return hit
            hit = self.by_run_name.get((str(task), str(worker or "")))
            if hit:
                return hit
            only = self.by_run.get(str(task))
            if only and len(only) == 1:
                return next(iter(only))
        return None


def analyse(events, workers, camp_rows):
    roster = Roster(workers, camp_rows)
    buckets = {"total_calls": 0, "discovery_excluded": 0, "unknown_attribution": 0,
               "attributed_not_fanout": 0, "sibling_calls": 0, "no_args": 0}
    per = {}        # campaign -> dict
    for call, outcome in pair_events(events):
        buckets["total_calls"] += 1
        tool = str(call.get("tool") or "")
        if tool.startswith(DISCOVERY_PREFIX):
            buckets["discovery_excluded"] += 1
            continue
        if call.get("attr") not in ATTRIBUTED_KINDS or not (call.get("task")
                                                            or call.get("worker")):
            buckets["unknown_attribution"] += 1
            continue
        hit = roster.resolve(call.get("task"), call.get("worker"))
        if hit is None:
            buckets["attributed_not_fanout"] += 1
            continue
        cid, child = hit
        buckets["sibling_calls"] += 1
        p = per.setdefault(cid, {"calls": [], "t0": None, "t1": None})
        ts = call.get("ts")
        if isinstance(ts, (int, float)):
            end = outcome.get("ts") if outcome and isinstance(outcome.get("ts"),
                                                              (int, float)) else ts
            p["t0"] = ts if p["t0"] is None else min(p["t0"], ts)
            p["t1"] = end if p["t1"] is None else max(p["t1"], end)
        key = _arg_key(call)
        if key is None:
            buckets["no_args"] += 1
        p["calls"].append((ts if isinstance(ts, (int, float)) else 0.0, child, key,
                           _duration(outcome)))

    rows = []
    for cid in sorted(set(roster.children) | set(per)):
        kids = roster.children.get(cid, {})
        completed = sum(1 for o in kids.values() if o in TERMINAL_OK)
        p = per.get(cid, {"calls": [], "t0": None, "t1": None})
        comparable = [c for c in p["calls"] if c[2] is not None]
        comparable.sort(key=lambda c: c[0])
        first = {}
        dup = dup_t = dup_untimed = 0
        for _ts, child, key, dur in comparable:
            owner = first.setdefault(key, child)
            if owner != child:
                dup += 1
                if dur is None:
                    dup_untimed += 1
                else:
                    dup_t += dur
        wall = (p["t1"] - p["t0"]) if p["t0"] is not None and p["t1"] is not None else None
        merges = roster.merges.get(cid, [])
        missing = sorted(k for k, o in kids.items() if o not in TERMINAL_OK)
        merge_failed = bool(merges) and (not any(o in TERMINAL_OK for o in merges)
                                         or bool(missing))
        rows.append({
            "campaign": cid, "children": len(kids), "completed": completed,
            "calls": len(p["calls"]), "comparable": len(comparable), "dup": dup,
            "dup_time_s": dup_t, "dup_untimed": dup_untimed,
            "wall_s": wall if wall and wall > 0 else None,
            "merge": bool(merges), "merge_failed": merge_failed,
            "merge_missing": len(missing) if merges else 0,
            "qualifies": completed >= 2 and len(p["calls"]) >= 1,
        })
    return {"buckets": buckets, "rows": rows, "known": len(roster.known),
            "merged_markers": len(roster.merged_marker)}


def verdict(summary):
    """(verdict line, thresholds explanation) from analyse()'s summary dict."""
    q = summary["m4"]
    if q < MIN_CAMPAIGNS:
        return "INSUFFICIENT (<%d campaigns)" % MIN_CAMPAIGNS
    if summary["merges"] == 0 or summary["dup_share"] is None:
        return "INSUFFICIENT (no merge or wall-clock evidence)"
    if summary["dup_share"] < DUP_TIME_SHARE_LIMIT and \
            summary["merge_loss_rate"] < MERGE_LOSS_LIMIT:
        return "LOW"
    return "NOTABLE"


def summarise(result):
    rows = result["rows"]
    qual = [r for r in rows if r["qualifies"]]
    comp = sum(r["comparable"] for r in qual)
    dup = sum(r["dup"] for r in qual)
    dup_t = sum(r["dup_time_s"] for r in qual)
    wall = sum(r["wall_s"] for r in qual if r["wall_s"])
    merges = sum(1 for r in qual if r["merge"])
    failed = sum(1 for r in qual if r["merge_failed"])
    lost = sum(1 for r in qual if r["merge_failed"] and r["merge_missing"] > 0)
    return {
        "m4": len(qual), "known": result["known"],
        "with_2_completed": sum(1 for r in rows if r["completed"] >= 2),
        "with_calls": sum(1 for r in rows if r["calls"] > 0),
        "comparable": comp, "dup": dup, "dup_rate": (dup / comp) if comp else None,
        "dup_time_s": dup_t, "wall_s": wall,
        "dup_share": (dup_t / wall) if wall else None,
        "dup_untimed": sum(r["dup_untimed"] for r in qual),
        "merges": merges, "merge_failed": failed, "merge_lost_info": lost,
        "merge_loss_rate": (lost / merges) if merges else None,
    }


def _pct(v):
    return "n/a" if v is None else "%.1f%%" % (100.0 * v)


def render(result, summary, top=50, n_transcripts=0):
    b = result["buckets"]
    lines = ["# Sibling duplication report", ""]
    lines += ["## Verdict", "", "**%s**" % verdict(summary), "",
              "Thresholds (script constants): at least %d qualifying campaigns; duplicate time "
              "< %s of campaign wall-clock AND merge loss attributable to missing child "
              "information < %s of merges (2 of 10) => LOW; otherwise NOTABLE."
              % (MIN_CAMPAIGNS, _pct(DUP_TIME_SHARE_LIMIT), _pct(MERGE_LOSS_LIMIT)), ""]
    lines += ["## Counts", "",
              "- ledger calls read: %d" % b["total_calls"],
              "- excluded gateway discovery calls: %d" % b["discovery_excluded"],
              "- unknown bucket (no or ambiguous attribution): %d" % b["unknown_attribution"],
              "- attributed but not mapped to a fan-out child: %d" % b["attributed_not_fanout"],
              "- sibling calls (attributed to a campaign child): %d" % b["sibling_calls"],
              "- sibling calls without comparable args (excluded from M1): %d" % b["no_args"],
              "- transcript files present (not read): %d" % n_transcripts, ""]
    lines += ["## Measures", "",
              "- M1 sibling duplicate-call rate: %s (%d duplicate / %d comparable calls)"
              % (_pct(summary["dup_rate"]), summary["dup"], summary["comparable"]),
              "- M2 duplicate time / campaign wall-clock: %s (%.1f s / %.1f s; wall-clock is "
              "the span of attributed calls; %d duplicate calls had no duration)"
              % (_pct(summary["dup_share"]), summary["dup_time_s"], summary["wall_s"],
                 summary["dup_untimed"]),
              "- M3 merge loss (proxy: merge worker not OK, or ran with >= 1 child slice not "
              "DONE): %d merges, %d failed, %d with missing child information (%s of merges)"
              % (summary["merges"], summary["merge_failed"], summary["merge_lost_info"],
                 _pct(summary["merge_loss_rate"])),
              "- M4 denominator: %d campaigns qualify (>= 2 completed children and attributed "
              "calls); %d campaigns known, %d with >= 2 completed children, %d with attributed "
              "calls" % (summary["m4"], summary["known"], summary["with_2_completed"],
                         summary["with_calls"]), ""]
    qual = [r for r in result["rows"] if r["qualifies"]][:max(0, top)]
    lines += ["## Per campaign (qualifying)", ""]
    if not qual:
        lines.append("none")
    else:
        lines += ["| campaign | children | completed | calls | comparable | dup | dup time s | "
                  "wall s | merge | merge failed |", "|---|---|---|---|---|---|---|---|---|---|"]
        for r in qual:
            lines.append("| %s | %d | %d | %d | %d | %d | %.1f | %s | %s | %s |" % (
                r["campaign"], r["children"], r["completed"], r["calls"], r["comparable"],
                r["dup"], r["dup_time_s"],
                "n/a" if r["wall_s"] is None else "%.1f" % r["wall_s"],
                "yes" if r["merge"] else "no", "yes" if r["merge_failed"] else "no"))
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only sibling duplication report.")
    ap.add_argument("--fleet-dir", default=None,
                    help="directory holding the fleet records (default: the live one, read-only)")
    ap.add_argument("--top", type=int, default=50)
    args = ap.parse_args(argv)
    # Resolved here, not as a module constant: nothing in this script writes, and a module-level
    # path to the live record directory would have to be classified by the isolation guard.
    fleet_dir = args.fleet_dir or os.path.join(REPO, ".fleet")
    events, workers, camps, n_tr = load_fleet(fleet_dir)
    result = analyse(events, workers, camps)
    summary = summarise(result)
    text = render(result, summary, top=args.top, n_transcripts=n_tr)
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.write(text.encode("ascii", "replace").decode("ascii"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
