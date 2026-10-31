# -*- coding: utf-8 -*-
"""Read-only report: from the shadow records of the tool-result cache (K3), how often a
read-only tool call repeated an earlier one, how much time a cache would have saved, and how
often the fresh answer differed from the one a cache would have served.

WHAT DECISION THIS SERVES. Whether it is worth building a cache that actually serves repeated
read-only tool results. relay/result_cache.py (setting `result_cache=shadow`) appends one row per
read-only tool call to .fleet/result_cache_shadow.jsonl; this script only reads those rows (and
the one rotated copy, `.1`) and writes nothing.

BACKGROUND. In the isolated-fleet experiment of 2026-10-20, sibling fan-out children repeated 135
tool calls whose arguments and results were identical (9.2% of the duplicate time, 1.8-18.5%
across intervals). This report asks whether the live fleet shows the same repeats, and whether
the repeated answers stay identical, which is what makes serving one safe.

WHAT THE SHADOW CAN AND CANNOT SAY. It says how many calls had a live earlier answer and how
long those calls took (the time a cache would have saved). It cannot say the saved time would
have been realised: every call still ran, and a served answer could change what the model does
next. It names no wall-clock share, because the rows hold no total tool time.

TERMS
  call      one row: a read-only tool call that ran.
  hit       the same (tool, argument hash) was answered within the TTL before this call.
  matched   a hit whose fresh result equalled the earlier one (a cache would have been right).
  differed  a hit whose fresh result did not equal the earlier one (a cache would have been wrong).

THE THRESHOLDS (stated here, not buried in the logic; each has a reason).
  calls     >= MIN_CALLS (200)   below this a handful of repeats moves the shares by whole points.
  runs      >= MIN_RUNS (3)      the measured repeats came from sibling fan-out of one run;
                                  rows from a single run cannot tell a habit from one run's quirk.
  hits      >= MIN_HITS (20)     the differed share is a share of hits; fewer hits cannot bound it.
When all three hold, the verdict is by the hits:
  differed share of hits > DIFFER_UNSAFE (5%)   UNSAFE_TO_SERVE: repeated answers often change, so
                                                 serving the earlier one would often be wrong.
  hit share of calls < HIT_WORTH (5%)           NOTHING_TO_GAIN: too few repeats for a cache to matter.
  otherwise                                     WORTH_A_SERVING_TEST: build a cache that serves, on a
                                                 paired comparison; do not switch on from shadow alone.
The two cut points (5%) are reporting boundaries chosen near the low end of the measured 1.8-18.5%
interval range; they are not a measured optimum. Anything else is INSUFFICIENT and the unmet
requirements are listed.

  python scripts/result_cache_report.py [--fleet-dir .fleet] [--format text|json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_NAME = "result_cache_shadow.jsonl"

MIN_CALLS = 200
MIN_RUNS = 3
MIN_HITS = 20
DIFFER_UNSAFE = 0.05
HIT_WORTH = 0.05

INSUFFICIENT, NOTHING_TO_GAIN, UNSAFE_TO_SERVE, WORTH_A_SERVING_TEST = (
    "INSUFFICIENT", "NOTHING_TO_GAIN", "UNSAFE_TO_SERVE", "WORTH_A_SERVING_TEST")


def read_rows(path):
    """(rows, unreadable): the rotated copy first, then the live file. A missing file is empty;
    a line that is not a JSON object is counted, not guessed at."""
    rows, bad = [], 0
    for p in (path + ".1", path):
        try:
            with open(p, "r", encoding="utf-8-sig", errors="replace") as fh:
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


def _share(n, d):
    return (n / d) if d else None


def _saved(r):
    v = r.get("saved_s")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def summarise(rows):
    calls = [r for r in rows if r.get("read_only", True)]
    hits = [r for r in calls if r.get("hit")]
    matched = [r for r in hits if r.get("matched")]
    differed = [r for r in hits if r.get("differed")]
    by_tool = {}
    for r in calls:
        e = by_tool.setdefault(str(r.get("tool") or ""),
                               {"calls": 0, "hits": 0, "differed": 0, "saved_s": 0.0})
        e["calls"] += 1
        if r.get("hit"):
            e["hits"] += 1
            e["saved_s"] = round(e["saved_s"] + _saved(r), 3)
            if r.get("differed"):
                e["differed"] += 1
    runs = {str(r.get("run_id") or "") for r in calls}
    runs.discard("")
    ts = [r["ts"] for r in rows if isinstance(r.get("ts"), (int, float))]
    return {"rows": len(rows), "calls": len(calls), "hits": len(hits),
            "hit_share": _share(len(hits), len(calls)),
            "matched": len(matched), "differed": len(differed),
            "differed_share": _share(len(differed), len(hits)),
            "saved_s": round(sum(_saved(r) for r in hits), 3),
            "by_tool": dict(sorted(by_tool.items())),
            "runs": len(runs), "first_ts": min(ts) if ts else None,
            "last_ts": max(ts) if ts else None}


def verdict(s):
    """(label, [unmet requirements]) from a summary."""
    unmet = []
    if s["calls"] < MIN_CALLS:
        unmet.append("read-only calls: %d of %d" % (s["calls"], MIN_CALLS))
    if s["runs"] < MIN_RUNS:
        unmet.append("distinct runs: %d of %d" % (s["runs"], MIN_RUNS))
    if s["hits"] < MIN_HITS:
        unmet.append("hits: %d of %d" % (s["hits"], MIN_HITS))
    if unmet:
        return INSUFFICIENT, unmet
    if (s["differed_share"] or 0.0) > DIFFER_UNSAFE:
        return UNSAFE_TO_SERVE, []
    if (s["hit_share"] or 0.0) < HIT_WORTH:
        return NOTHING_TO_GAIN, []
    return WORTH_A_SERVING_TEST, []


def _pct(v):
    return "n/a" if v is None else "%.1f%%" % (100.0 * v)


def render(s, label, unmet, unreadable=0):
    lines = ["# Tool-result cache, shadow records", "",
             "Read-only. A shadow row says a read-only tool call repeated an earlier one and "
             "whether the fresh answer matched; every call still ran, so the saved time is what "
             "a cache could have saved, not time that was saved.", "",
             "- rows: %d (unreadable lines skipped: %d); distinct runs: %d"
             % (s["rows"], unreadable, s["runs"]),
             "- read-only calls: %d" % s["calls"],
             "- hits (same tool and arguments answered within the TTL): %d (%s of calls)"
             % (s["hits"], _pct(s["hit_share"])),
             "- matched: %d; differed: %d (%s of hits)"
             % (s["matched"], s["differed"], _pct(s["differed_share"])),
             "- time a cache would have saved: %.1f s" % s["saved_s"], "", "## By tool", ""]
    lines += ["- %s: %d calls, %d hits, %d differed, %.1f s"
              % (k or "(none)", v["calls"], v["hits"], v["differed"], v["saved_s"])
              for k, v in s["by_tool"].items()] or ["none"]
    lines += ["", "## Verdict: %s" % label, ""]
    if unmet:
        lines.append("Not enough data to decide. Still needed:")
        lines += ["- " + u for u in unmet]
    elif label == UNSAFE_TO_SERVE:
        lines.append("Enough data. More than %d%% of hits had a different fresh answer, so serving "
                     "the earlier one would often be wrong." % int(DIFFER_UNSAFE * 100))
    elif label == NOTHING_TO_GAIN:
        lines.append("Enough data. Under %d%% of read-only calls repeat an earlier one; a cache "
                     "that serves would change little." % int(HIT_WORTH * 100))
    else:
        lines.append("Enough data. Repeats are common and their answers match. Next step: a "
                     "paired test of a cache that serves. Do not switch serving on from shadow "
                     "rows alone.")
    lines += ["", "Threshold: >= %d read-only calls, >= %d runs, >= %d hits."
              % (MIN_CALLS, MIN_RUNS, MIN_HITS)]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only tool-result cache shadow report.")
    ap.add_argument("--fleet-dir", default=None,
                    help="directory holding the fleet records (default: the live one, read-only)")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    ap.add_argument("--same-fingerprint", action="store_true",
                    help="compare only hits with two available, equal pre-call fingerprints")
    ap.add_argument("--by-tool", action="store_true", help="show per-tool comparison rates")
    args = ap.parse_args(argv)
    # Resolved here, not as a module constant: nothing in this script writes, and a module-level
    # path to the live record directory would have to be classified by the isolation guard.
    fleet_dir = args.fleet_dir or os.path.join(REPO, ".fleet")
    rows, bad = read_rows(os.path.join(fleet_dir, LOG_NAME))
    if args.same_fingerprint:
        pairs = [r for r in rows if r.get("read_only", True) and r.get("hit")
                 and isinstance(r.get("fingerprint"), dict) and r["fingerprint"]
                 and r.get("fingerprint") == r.get("previous_fingerprint")
                 and isinstance(r.get("result_hash"), str) and r.get("result_hash")
                 and isinstance(r.get("previous_result_hash"), str) and r.get("previous_result_hash")]
        by_tool = {}
        for r in pairs:
            item = by_tool.setdefault(str(r.get("tool") or ""), {"pairs": 0, "differed": 0})
            item["pairs"] += 1
            item["differed"] += r["result_hash"] != r["previous_result_hash"]
        for item in by_tool.values():
            item["differed_share"] = _share(item["differed"], item["pairs"])
        differed = sum(item["differed"] for item in by_tool.values())
        summary = {"pairs": len(pairs), "differed": differed,
                   "differed_share": _share(differed, len(pairs)), "unreadable": bad}
        if args.by_tool:
            summary["by_tool"] = dict(sorted(by_tool.items()))
        if args.format == "json":
            text = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        else:
            text = ("Same pre-call fingerprint: %d pairs, %d differed (%s).\n"
                    "Metadata fingerprints are incomplete and do not prove unchanged state or safe serving.\n"
                    % (len(pairs), differed, _pct(summary["differed_share"])))
            if args.by_tool:
                for name, item in sorted(by_tool.items()):
                    text += "- %s: %d pairs, %d differed (%s)\n" % (
                        name, item["pairs"], item["differed"], _pct(item["differed_share"]))
        sys.stdout.write(text)
        return 0
    s = summarise(rows)
    label, unmet = verdict(s)
    if args.format == "json":
        text = json.dumps({"summary": s, "verdict": label, "unmet": unmet, "unreadable": bad},
                          ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    else:
        text = render(s, label, unmet, bad)
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.write(text.encode("ascii", "replace").decode("ascii"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
