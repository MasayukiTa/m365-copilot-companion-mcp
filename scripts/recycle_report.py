"""What the conversation recycle actually frees, or why that is still unknown.

THE QUESTION. The bridge keeps one long-lived Copilot conversation and recycles it every so
many turns. Whether the resident page is worth keeping depends on how much a recycle gives
back: if it frees most of what the page had accumulated, keeping the page is cheap and the
recycle is the release valve. If it frees nothing, the page is a slow leak and the design
should change.

WHY THERE MAY BE NOTHING TO REPORT. Samples are written by real use -- one row per recycle,
and a recycle only happens after enough turns have gone through the bridge. A machine that has
not held a long conversation has no rows, and a bridge restarted since the last recycle has
none either. That is an ordinary state, not a fault.

WHY THIS SCRIPT EXISTS ANYWAY. Without it the absence is silent: the file simply is not there,
and a decision waiting on data that nobody can see is a decision that quietly never gets made.
It has sat unresolved through several sessions for exactly that reason. This turns "no file"
into a sentence that says what is missing and what would produce it.

A row whose after_mb is null is NOT a zero. It means the measurement could not be taken --
the browser was gone, or the port owner could not be resolved -- and averaging it in as zero
would report "the recycle frees nothing" on the strength of a failed reading.

  python scripts/recycle_report.py
"""
from __future__ import annotations

import io
import json
import os
import statistics as st
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(REPO, ".fleet", "recycle_samples.jsonl")


def load(path=PATH):
    """(rows, unreadable_lines). A corrupt tail must not hide the rows before it."""
    rows, bad = [], 0
    if not os.path.exists(path):
        return rows, bad
    for line in io.open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            bad += 1
    return rows, bad


def summarise(rows):
    """A verdict, or the reason there is not one yet."""
    measured = [r for r in rows if r.get("freed_mb") is not None]
    out = {"rows": len(rows), "measured": len(measured), "verdict": None, "why": ""}
    if not rows:
        out["why"] = ("no recycle has been recorded yet -- the bridge recycles its conversation "
                      "after a number of turns, so this fills in with ordinary use")
        return out
    if not measured:
        out["why"] = ("%d recycle(s) recorded but none could be measured; the browser's working "
                      "set was unreadable at the time" % len(rows))
        return out
    freed = [r["freed_mb"] for r in measured]
    out["freed_median_mb"] = round(st.median(freed), 1)
    out["freed_min_mb"], out["freed_max_mb"] = round(min(freed), 1), round(max(freed), 1)
    out["before_median_mb"] = round(st.median([r["before_mb"] for r in measured]), 1)
    if len(measured) < 5:
        out["why"] = ("%d measured recycle(s); too few to read a median against a browser whose "
                      "idle spread is in the hundreds of MB" % len(measured))
        return out
    # A recycle that gives back a large share of what the page held makes the resident page a
    # cheap design. One that gives back little makes it a leak that nothing currently drains.
    share = out["freed_median_mb"] / out["before_median_mb"] if out["before_median_mb"] else 0.0
    out["freed_share"] = round(share, 3)
    if share >= 0.25:
        out["verdict"] = "recycle-releases"
        out["why"] = "the recycle gives back a quarter or more of what the page was holding"
    elif out["freed_median_mb"] <= 50:
        out["verdict"] = "recycle-does-not-release"
        out["why"] = "the recycle frees under 50 MB; the resident page is not being drained"
    else:
        out["why"] = "between the two readings; more samples would separate them"
    return out


def main(argv=None):                                            # pragma: no cover
    rows, bad = load()
    out = summarise(rows)
    if bad:
        out["unreadable_lines"] = bad
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if rows:
        print("\nlast rows:")
        for r in rows[-5:]:
            print("  %s turns=%s before=%s after=%s freed=%s" % (
                time.strftime("%m-%d %H:%M", time.localtime(r.get("ts", 0))),
                r.get("turns"), r.get("before_mb"), r.get("after_mb"), r.get("freed_mb")))
    return 0


if __name__ == "__main__":                                      # pragma: no cover
    sys.exit(main() or 0)
