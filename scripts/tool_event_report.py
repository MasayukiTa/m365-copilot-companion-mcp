# -*- coding: utf-8 -*-
"""Read-only timing and attribution report over the tool-call ledger (.fleet/tool_events.jsonl).

WHAT DECISION THIS SERVES. Whether a change to how tools are offered is worth building depends
on where the wall-clock time of a worker turn goes. If model generation is most of the wait, the
time spent in tool calls and the gaps between them is small and there is little to win. This
report measures the tool-side half: how long calls take, how long the gaps between calls are,
how much of the traffic is catalogue/signature lookup, and how much of it can be attributed to a
task and a worker at all. The generation-time half is recorded per turn by relay_fleet as the
`turn_wait_s` metric in the transcripts; the two are joined by whoever makes the decision.

HONESTY RULES.
  * A call with no outcome (orphan) has no duration. It is counted, never averaged in as zero.
  * Rows written before timing existed carry only `duration_s` and `ts`; they are used where
    they can be and counted as `legacy` so a reader knows how much of the sample is old.
  * Attribution that is empty stays empty. The fill rate is reported as measured.
  * Gaps are taken within one MCP session (falling back to one process) and only between calls
    that are comparable: monotonic stamps inside one process, wall clock otherwise.

Read-only: nothing is written, and the ledger path is resolved from tools.tool_ledger.

  python scripts/tool_event_report.py [--path .fleet/tool_events.jsonl] [--top 20] [--since EPOCH]
"""
from __future__ import annotations

import argparse
import math
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

#: Gateway-internal lookups written by main.py's _log_discovery: catalogue, signature, refused.
DISCOVERY_PREFIX = "call_tool."

#: How a row's task/worker was obtained (the ledger's `attr` field); rows without it are "none".
#: explicit = the caller passed it; session = the worker declared it via the turn-loop protocol;
#: window = exactly one worker's turn was in flight (coordinator's turn_context);
#: session-window = several were, but this MCP session was bound earlier by a `window` match;
#: ambiguous = several were and nothing disambiguates: task/worker are deliberately EMPTY.
ATTR_KINDS = ("explicit", "session", "window", "session-window", "ambiguous")


def percentile(values, q):
    """Nearest-rank percentile of a list of numbers, or None when empty."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = max(1, int(math.ceil(q / 100.0 * len(vals))))
    return vals[min(k, len(vals)) - 1]


def _fmt(v):
    return "-" if v is None else ("%.3f" % v)


def pair_events(rows):
    """[(call_row, outcome_row_or_None)] in call order."""
    outcomes = {}
    for r in rows:
        if r.get("event") == "outcome":
            outcomes[r.get("id")] = r
    return [(r, outcomes.get(r.get("id"))) for r in rows if r.get("event") == "call"]


def duration_of(call, outcome):
    """Seconds the call took, or None. Prefers the monotonic figure, then the caller's own."""
    if not outcome:
        return None
    for key in ("dur_mono_s", "duration_s"):
        v = outcome.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def has_timing(outcome):
    return bool(outcome) and isinstance(outcome.get("dur_mono_s"), (int, float))


def gaps(pairs):
    """Seconds between the end of one call and the start of the next on the same session.

    Keyed by session fingerprint, or by process when there is none. Monotonic stamps are only
    subtracted inside one process; across a restart the wall clock is used instead. Negative
    differences (overlapping concurrent calls) are dropped rather than clamped.
    """
    last = {}
    out = []
    for call, outcome in pairs:
        if not outcome:
            continue
        key = call.get("session") or ("proc:" + str(call.get("proc") or ""))
        prev = last.get(key)
        if prev is not None:
            p_end, p_end_mono, p_proc = prev
            if call.get("proc") and call.get("proc") == p_proc \
                    and isinstance(call.get("mono"), (int, float)) and p_end_mono is not None:
                gap = call["mono"] - p_end_mono
            else:
                gap = float(call.get("ts", 0)) - p_end
            if gap >= 0:
                out.append(gap)
        last[key] = (float(outcome.get("ts", call.get("ts", 0))),
                     outcome.get("mono") if isinstance(outcome.get("mono"), (int, float)) else None,
                     call.get("proc"))
    return out


def analyse(rows, top=20):
    pairs = pair_events(rows)
    per_tool, per_task = {}, {}
    n_task = n_worker = n_session_attr = orphans = legacy = discovery = 0
    attr_kinds = dict.fromkeys(ATTR_KINDS + ("none",), 0)
    for call, outcome in pairs:
        tool = str(call.get("tool") or "")
        dur = duration_of(call, outcome)
        per_tool.setdefault(tool, []).append(dur)
        if not outcome:
            orphans += 1
        elif not has_timing(outcome):
            legacy += 1
        if tool.startswith(DISCOVERY_PREFIX):
            discovery += 1
        task = str(call.get("task") or "")
        kind = call.get("attr") if call.get("attr") in ATTR_KINDS else "none"
        attr_kinds[kind] += 1
        if task:
            n_task += 1
            if call.get("attr") == "session":
                n_session_attr += 1
        if call.get("worker"):
            n_worker += 1
        slot = per_task.setdefault(task or "(none)", {"calls": 0, "failed": 0, "secs": 0.0,
                                                      "durs": []})
        slot["calls"] += 1
        if outcome is not None and not outcome.get("ok", True):
            slot["failed"] += 1
        if dur is not None:
            slot["secs"] += dur
            slot["durs"].append(dur)
    total = len(pairs)
    return {
        "calls": total, "orphans": orphans, "legacy": legacy, "discovery": discovery,
        "attr_kinds": attr_kinds, "task_filled": n_task, "worker_filled": n_worker, "session_attributed": n_session_attr,
        "per_tool": per_tool, "per_task": per_task, "gaps": gaps(pairs), "top": top,
    }


def render(a):
    n = a["calls"]

    def share(x):
        return "%.1f%%" % (100.0 * x / n) if n else "-"

    lines = ["# Tool event report", ""]
    lines.append("- calls: %d (no outcome: %d, rows without monotonic timing: %d)"
                 % (n, a["orphans"], a["legacy"]))
    lines.append("- discovery calls (`%s*`): %d (%s)" % (DISCOVERY_PREFIX, a["discovery"],
                                                         share(a["discovery"])))
    lines.append("- task filled: %d (%s), of which from the worker's own session declaration: %d"
                 % (a["task_filled"], share(a["task_filled"]), a["session_attributed"]))
    lines.append("- worker filled: %d (%s)" % (a["worker_filled"], share(a["worker_filled"])))
    lines.append("- attribution by kind: " + ", ".join(
        "%s %d (%s)" % (k, v, share(v)) for k, v in a["attr_kinds"].items()))
    lines.append("- inter-call gap p50/p95 (s): %s / %s over %d gaps"
                 % (_fmt(percentile(a["gaps"], 50)), _fmt(percentile(a["gaps"], 95)),
                    len(a["gaps"])))
    lines += ["", "## Per tool duration (s)", "",
              "| tool | calls | timed | p50 | p95 |", "|---|---:|---:|---:|---:|"]
    for tool, durs in sorted(a["per_tool"].items(), key=lambda kv: -len(kv[1])):
        timed = [d for d in durs if d is not None]
        lines.append("| %s | %d | %d | %s | %s |" % (tool or "(blank)", len(durs), len(timed),
                                                      _fmt(percentile(timed, 50)),
                                                      _fmt(percentile(timed, 95))))
    lines += ["", "## Per task (top %d by calls)" % a["top"], "",
              "| task | calls | failed | tool time (s) | p50 | p95 |",
              "|---|---:|---:|---:|---:|---:|"]
    ranked = sorted(a["per_task"].items(), key=lambda kv: -kv[1]["calls"])[:a["top"]]
    for task, s in ranked:
        lines.append("| %s | %d | %d | %.1f | %s | %s |"
                     % (task, s["calls"], s["failed"], s["secs"],
                        _fmt(percentile(s["durs"], 50)), _fmt(percentile(s["durs"], 95))))
    return "\n".join(lines) + "\n"


def main(argv=None):
    from tools import tool_ledger
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--path", default=None, help="ledger file (default: the live ledger)")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--since", type=float, default=0.0, help="ignore rows before this epoch ts")
    args = ap.parse_args(argv)
    rows = tool_ledger.read(args.path)
    if args.since:
        rows = [r for r in rows if float(r.get("ts", 0) or 0) >= args.since]
    sys.stdout.write(render(analyse(rows, top=args.top)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
