# -*- coding: utf-8 -*-
"""Read-only report: the COST (wall-clock seconds) siblings spend on duplicated tool calls,
weighted by how expensive each call actually was, so \"is a board worth its cost\" can be answered.

WHY THIS EXISTS (B3, communication telemetry). B0 (scripts/sibling_dup_report.py) measured whether
fan-out siblings repeat each other's calls, but it counted every repeat equally and the live data it
could attribute was dominated by millisecond read_file calls, so its duplicate-time share (~0.3%)
could never cross a 5% bar -- a limit of the harness, not a finding. A board that lets siblings share
work only pays for itself if the DUPLICATED work it would remove is EXPENSIVE (web search, long
shell/python, odbc), not cheap reads. This script therefore weights every duplicate by its measured
duration and reports the cost of duplication, and separately the cost confined to expensive tools.
It only measures; it changes nothing and writes nothing.

INPUTS (all read-only, under one --fleet-dir; default the live .fleet):
  tool_events.jsonl  call/outcome rows (tool, args digests, ts, task, worker, attr; and, on newer
                     coordinator-labelled rows, campaign_id/subtask_id/role). Durations come from
                     the paired outcome row (dur_mono_s / duration_s).
  status.json        workers: campaign_id, role, outcome, jid, task_id, run_id, name.
  campaigns.jsonl    campaign headers, one row per subtask (task_id), child_result and merge markers.

ATTRIBUTION IS REUSED, NOT REINVENTED. The campaign<-call mapping is sibling_dup_report.Roster, so
this report and B0 agree on who a call belongs to. A call this report cannot map to a fan-out child
goes to a named bucket and is NEVER counted as a duplicate or as a success.

MEASURES (cost = measured wall-clock seconds of the call, from its outcome)
  C1  duplicate-call cost: for sibling calls sharing one campaign, the SECOND and later issuers of an
      identical (tool + argument-digest) key are duplicates; their summed duration is the duplicate
      cost. The first issuer is never a duplicate; the same child repeating itself is not sibling
      duplication. A duplicate whose outcome carried no duration is counted but adds 0 cost and is
      reported separately (never averaged as zero).
  C2  duplicate cost confined to EXPENSIVE tools (EXPENSIVE_TOOLS or any tool whose per-call mean
      duration in this dataset is >= EXPENSIVE_MEAN_S). This is the part a board could actually save.
  C3  total attributed sibling-call cost (denominator for \"what share of sibling work is duplicated\").
  C4  denominator: campaigns with >= 2 completed children AND >= 1 attributed sibling call.

HONESTY. If too few campaigns qualify, or no sibling call can be attributed at all (the live schema
has empty task and a tab-local worker on tool_events rows, so older campaigns do not map), the
verdict is INSUFFICIENT and names which. A board decision is not made from an unmeasured quantity.

  python scripts/sibling_cost_report.py [--fleet-dir .fleet] [--top 50]
"""
from __future__ import annotations

import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from scripts import sibling_dup_report as B0  # noqa: E402  (reuse its loaders and Roster)

#: Tools known to be expensive regardless of this dataset's mean (I/O, network, subprocess, db).
EXPENSIVE_TOOLS = (
    "web_search", "web_fetch", "shell_exec", "run_python", "run_in_background",
    "odbc_query", "odbc_to_excel", "find_files", "glob", "job_wait",
)
#: A tool also counts as expensive when its mean measured duration here is at least this many seconds.
EXPENSIVE_MEAN_S = 2.0

#: Verdict thresholds (stated, not buried). A board is worth considering only when the expensive
#: duplicate cost is a real share of sibling work AND the absolute seconds are non-trivial.
MIN_CAMPAIGNS = 20
EXPENSIVE_DUP_SHARE_LIMIT = 0.05   # expensive duplicate cost < 5% of sibling cost => LOW


def expensive_tools(pairs):
    """Set of tool names that are expensive: in EXPENSIVE_TOOLS, or mean duration >= EXPENSIVE_MEAN_S."""
    tot = {}
    cnt = {}
    for call, outcome in pairs:
        tool = str(call.get("tool") or "")
        if not tool:
            continue
        d = B0._duration(outcome)
        if d is None:
            continue
        tot[tool] = tot.get(tool, 0.0) + d
        cnt[tool] = cnt.get(tool, 0) + 1
    costly = set(EXPENSIVE_TOOLS)
    for tool, c in cnt.items():
        if c and (tot[tool] / c) >= EXPENSIVE_MEAN_S:
            costly.add(tool)
    return costly


def analyse(events, workers, camp_rows, windows=None):
    """Cost-weighted sibling duplication. Attribution via sibling_dup_report.Roster (shared truth).

    `windows` (a relay.turn_context_join.WindowIndex) lets the shared Roster attribute calls the
    MCP session could not label, by joining them to the coordinator's turn windows. With
    windows=None the attribution is exactly B0's.
    """
    roster = B0.Roster(workers, camp_rows, windows=windows)
    pairs = B0.pair_events(events)
    costly = expensive_tools(pairs)
    buckets = {"total_calls": 0, "discovery_excluded": 0, "unknown_attribution": 0,
               "attributed_not_fanout": 0, "sibling_calls": 0, "no_args": 0,
               "window_attributed": 0}
    per = {}  # campaign -> {"calls": [(ts, child, key, tool, dur)]}
    for call, outcome in pairs:
        buckets["total_calls"] += 1
        tool = str(call.get("tool") or "")
        if tool.startswith(B0.DISCOVERY_PREFIX):
            buckets["discovery_excluded"] += 1
            continue
        if call.get("attr") not in B0.ATTRIBUTED_KINDS or not (call.get("task")
                                                               or call.get("worker")):
            buckets["unknown_attribution"] += 1
            continue
        _wh_before = roster.window_hits
        hit = roster.resolve_call(call)
        if hit is None:
            buckets["attributed_not_fanout"] += 1
            continue
        if roster.window_hits > _wh_before:
            buckets["window_attributed"] += 1
        cid, child = hit
        buckets["sibling_calls"] += 1
        key = B0._arg_key(call)
        if key is None:
            buckets["no_args"] += 1
        ts = call.get("ts")
        per.setdefault(cid, {"calls": []})["calls"].append(
            (ts if isinstance(ts, (int, float)) else 0.0, child, key, tool,
             B0._duration(outcome)))

    rows = []
    for cid in sorted(set(roster.children) | set(per)):
        kids = roster.children.get(cid, {})
        completed = sum(1 for o in kids.values() if o in B0.TERMINAL_OK)
        calls = per.get(cid, {"calls": []})["calls"]
        comparable = sorted([c for c in calls if c[2] is not None], key=lambda c: c[0])
        first = {}
        sib_cost = 0.0
        dup = 0
        dup_cost = 0.0
        dup_untimed = 0
        exp_dup = 0
        exp_dup_cost = 0.0
        exp_sib_cost = 0.0
        for _ts, child, key, tool, dur in comparable:
            cost = dur or 0.0
            sib_cost += cost
            if tool in costly:
                exp_sib_cost += cost
            owner = first.setdefault(key, child)
            if owner == child:
                continue
            dup += 1
            if dur is None:
                dup_untimed += 1
            dup_cost += cost
            if tool in costly:
                exp_dup += 1
                exp_dup_cost += cost
        merges = roster.merges.get(cid, [])
        missing = sorted(k for k, o in kids.items() if o not in B0.TERMINAL_OK)
        merge_failed = bool(merges) and (not any(o in B0.TERMINAL_OK for o in merges)
                                         or bool(missing))
        rows.append({
            "campaign": cid, "children": len(kids), "completed": completed,
            "calls": len(calls), "comparable": len(comparable),
            "sib_cost_s": sib_cost, "exp_sib_cost_s": exp_sib_cost,
            "dup": dup, "dup_cost_s": dup_cost, "dup_untimed": dup_untimed,
            "exp_dup": exp_dup, "exp_dup_cost_s": exp_dup_cost,
            "merge": bool(merges), "merge_failed": merge_failed,
            "qualifies": completed >= 2 and len(calls) >= 1,
        })
    return {"buckets": buckets, "rows": rows, "known": len(roster.known),
            "costly_tools": sorted(costly)}


def summarise(result):
    qual = [r for r in result["rows"] if r["qualifies"]]
    sib = sum(r["sib_cost_s"] for r in qual)
    dupc = sum(r["dup_cost_s"] for r in qual)
    exp_sib = sum(r["exp_sib_cost_s"] for r in qual)
    exp_dupc = sum(r["exp_dup_cost_s"] for r in qual)
    return {
        "m4": len(qual), "known": result["known"],
        "with_2_completed": sum(1 for r in result["rows"] if r["completed"] >= 2),
        "with_calls": sum(1 for r in result["rows"] if r["calls"] > 0),
        "sibling_calls": sum(r["calls"] for r in result["rows"]),
        "comparable": sum(r["comparable"] for r in qual),
        "dup": sum(r["dup"] for r in qual), "dup_cost_s": dupc,
        "dup_untimed": sum(r["dup_untimed"] for r in qual),
        "sib_cost_s": sib, "dup_cost_share": (dupc / sib) if sib else None,
        "exp_dup": sum(r["exp_dup"] for r in qual), "exp_dup_cost_s": exp_dupc,
        "exp_sib_cost_s": exp_sib,
        "exp_dup_share": (exp_dupc / exp_sib) if exp_sib else None,
    }


def verdict(summary):
    """(verdict line) from summarise()'s dict. Never a board decision from an unmeasured quantity."""
    if summary["m4"] < MIN_CAMPAIGNS:
        if summary["sibling_calls"] == 0:
            return ("INSUFFICIENT (attribution: no sibling call maps to a fan-out child; live "
                    "tool_events rows carry an empty task and a tab-local worker, so past "
                    "campaigns do not map -- a campaign_id/subtask_id on the call row is the "
                    "prerequisite)")
        return "INSUFFICIENT (<%d qualifying campaigns)" % MIN_CAMPAIGNS
    if summary["exp_sib_cost_s"] <= 0 or summary["exp_dup_share"] is None:
        return "INSUFFICIENT (no expensive sibling cost measured)"
    if summary["exp_dup_share"] < EXPENSIVE_DUP_SHARE_LIMIT:
        return "LOW"
    return "NOTABLE"


def _pct(v):
    return "n/a" if v is None else "%.1f%%" % (100.0 * v)


def render(result, summary, top=50):
    b = result["buckets"]
    lines = ["# Sibling duplication COST report (B3 communication telemetry)", ""]
    lines += ["## Verdict", "", "**%s**" % verdict(summary), "",
              "Thresholds (script constants): at least %d qualifying campaigns; expensive "
              "duplicate cost < %s of expensive sibling cost => LOW; otherwise NOTABLE. "
              "Expensive = %s, or any tool whose mean duration here is >= %.1fs."
              % (MIN_CAMPAIGNS, _pct(EXPENSIVE_DUP_SHARE_LIMIT), ", ".join(EXPENSIVE_TOOLS),
                 EXPENSIVE_MEAN_S), ""]
    lines += ["## Counts", "",
              "- ledger calls read: %d" % b["total_calls"],
              "- excluded gateway discovery calls: %d" % b["discovery_excluded"],
              "- unknown bucket (no/ambiguous attribution): %d" % b["unknown_attribution"],
              "- attributed but not mapped to a fan-out child: %d" % b["attributed_not_fanout"],
              "- sibling calls (attributed to a campaign child): %d" % b["sibling_calls"],
              "- of those, attributed via a coordinator turn window: %d"
              % b.get("window_attributed", 0),
              "- sibling calls without comparable args: %d" % b["no_args"],
              "- tools treated as expensive: %s" % ", ".join(result["costly_tools"]), ""]
    lines += ["## Measures (cost = measured wall-clock seconds)", "",
              "- C1 duplicate-call cost: %.1f s over %d duplicate calls (%d had no duration); "
              "%s of attributed sibling cost (%.1f s)"
              % (summary["dup_cost_s"], summary["dup"], summary["dup_untimed"],
                 _pct(summary["dup_cost_share"]), summary["sib_cost_s"]),
              "- C2 EXPENSIVE duplicate cost: %.1f s over %d expensive duplicate calls; "
              "%s of expensive sibling cost (%.1f s)"
              % (summary["exp_dup_cost_s"], summary["exp_dup"],
                 _pct(summary["exp_dup_share"]), summary["exp_sib_cost_s"]),
              "- C4 denominator: %d campaigns qualify (>= 2 completed children and attributed "
              "calls); %d known, %d with >= 2 completed children, %d with attributed calls"
              % (summary["m4"], summary["known"], summary["with_2_completed"],
                 summary["with_calls"]), ""]
    qual = [r for r in result["rows"] if r["qualifies"]][:max(0, top)]
    lines += ["## Per campaign (qualifying)", ""]
    if not qual:
        lines.append("none")
    else:
        lines += ["| campaign | children | completed | calls | dup | dup cost s | "
                  "exp dup | exp dup cost s | merge failed |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for r in qual:
            lines.append("| %s | %d | %d | %d | %d | %.1f | %d | %.1f | %s |" % (
                r["campaign"], r["children"], r["completed"], r["calls"], r["dup"],
                r["dup_cost_s"], r["exp_dup"], r["exp_dup_cost_s"],
                "yes" if r["merge_failed"] else "no"))
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only cost-weighted sibling duplication report.")
    ap.add_argument("--fleet-dir", default=None,
                    help="directory holding the fleet records (default: the live one, read-only)")
    ap.add_argument("--top", type=int, default=50)
    args = ap.parse_args(argv)
    fleet_dir = args.fleet_dir or os.path.join(REPO, ".fleet")
    events, workers, camps, _n_tr = B0.load_fleet(fleet_dir)
    # Join calls the MCP session could not label to the coordinator's turn windows (read-only;
    # an absent turn_context.jsonl yields an empty index and the report is unchanged).
    try:
        from relay import turn_context_join
        windows = turn_context_join.load_windows(fleet_dir)
    except Exception:
        windows = None
    result = analyse(events, workers, camps, windows=windows)
    summary = summarise(result)
    text = render(result, summary, top=args.top)
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.write(text.encode("ascii", "replace").decode("ascii"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
