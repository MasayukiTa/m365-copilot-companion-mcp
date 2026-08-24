"""Read the absolute trace of a diagnostic run and answer the reviewers' question.

WHAT IS BEING ASKED. Every arm of the series is preceded by a warm-up over the TABS route, and
the baseline is taken after it. Two reviewers agreed that is the central problem and disagreed
about its direction. One held that the tabs arm's bill is missing a renderer the warm-up
already paid for -- 170 to 255 MB by the per-arm detail -- so the real cost of switching is
larger than measured. The other held the direction is not identifiable in advance and that the
mechanism worth fearing runs the other way: the warm renderer decays during a socket arm
because nothing keeps it alive, so socket looks cheap for reasons unrelated to sockets.

The diagnostic runs a null with the warm-up off, which makes its FIRST arm cold and its second
warm, and then sits still. So the trace holds three quantities the series could not produce:

  cold      what one arm of this transport costs a browser that has just started
  warm      what the same work costs once the first arm has already paid for the pool
  decay     what the browser gives back on its own, with neither transport running

`cold - warm` prices the warm-up: it is what the series was NOT charging either arm. `decay`
prices the other objection: how much of an idle arm's apparent cheapness is simply the browser
letting go of what the warm-up left behind.

ABSOLUTE, THROUGHOUT. Every number here is a working-set total, not a difference from a
baseline, because what the baseline contains is the thing in dispute.

  python scripts/diag_report.py
"""
from __future__ import annotations

import csv
import glob
import io
import json
import os
import statistics as st
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIAG = os.path.join(REPO, ".fleet", "diag")


def read_trace(path):
    """[(ts, total_mb)] for the samples that had a browser to measure."""
    out = []
    for r in csv.DictReader(io.open(path, encoding="utf-8")):
        if r.get("total_mb"):
            out.append((float(r["ts"]), float(r["total_mb"])))
    return out


def window(trace, t0, t1):
    return [v for ts, v in trace if t0 <= ts <= t1]


def analyse(events_path):
    """One run's cold/warm/decay, or the reason it cannot be read."""
    with io.open(events_path, encoding="utf-8") as fh:
        ev = json.load(fh)
    base = events_path[: -len("_events.json")]
    trace = read_trace(base + "_ws.csv")
    at = {e["event"]: e["ts"] for e in ev["events"]}
    out = {"transport": ev["transport"], "samples": len(trace),
           "warmup": ev.get("warmup"), "gain_mb": ev.get("memory_gain_mb")}
    if not trace or "run_start" not in at:
        out["why"] = "no usable trace"
        return out

    fresh = window(trace, at["browser_rebuilt"], at.get("settled_fresh", at["run_start"]))
    out["fresh_mb"] = round(st.median(fresh), 1) if fresh else None

    # The two arms split the run. Their own reported wall times say where.
    c, d = ev.get("control") or {}, ev.get("candidate") or {}
    first, second = (d, c) if (ev.get("arm_order", "") or "").startswith("candidate") else (c, d)
    w1, w2 = float(first.get("wall_s") or 0), float(second.get("wall_s") or 0)
    t_run0, t_run1 = at["run_start"], at["run_end"]
    if w1 and w2 and (w1 + w2) <= (t_run1 - t_run0) + 120:
        split = t_run0 + (t_run1 - t_run0) * (w1 / (w1 + w2))
    else:
        split = (t_run0 + t_run1) / 2.0
    cold = window(trace, t_run0, split)
    warm = window(trace, split, t_run1)
    # THE PEAK IS THE POINT. The floor a per-tab budget has to clear is the highest the
    # browser ever went, not where it settled afterwards.
    out["cold_peak_mb"] = round(max(cold), 1) if cold else None
    out["warm_peak_mb"] = round(max(warm), 1) if warm else None
    if out["cold_peak_mb"] and out["fresh_mb"]:
        out["cold_cost_mb"] = round(out["cold_peak_mb"] - out["fresh_mb"], 1)
    if out["cold_peak_mb"] and out["warm_peak_mb"]:
        out["what_the_warmup_hides_mb"] = round(out["cold_peak_mb"] - out["warm_peak_mb"], 1)

    # COLD MINUS WARM CONFLATES TWO OPPOSITE THINGS and cannot be read on its own. Arm 2's peak
    # is quoted over the ORIGINAL fresh baseline, but arm 2 begins on top of whatever arm 1 left
    # undecayed. So a figure near zero can be a real reuse discount cancelling residue inflation
    # rather than neither being present. Measuring arm 2's rise over its OWN starting point --
    # which is the series estimator, applied inside the diagnostic -- separates them.
    at_split = [v for ts, v in trace if abs(ts - split) <= 8.0]
    if at_split and warm:
        out["warm_start_mb"] = round(st.median(at_split), 1)
        out["warm_rise_over_own_start_mb"] = round(max(warm) - out["warm_start_mb"], 1)
    if cold and out["fresh_mb"]:
        out["cold_rise_over_own_start_mb"] = round(max(cold) - out["fresh_mb"], 1)

    # A cheap arm that quietly did less work is the oldest false economy there is.
    out["done"] = {"first": first.get("done"), "second": second.get("done"),
                   "goals": first.get("goals")}
    out["wall_s"] = {"first": first.get("wall_s"), "second": second.get("wall_s")}

    # THE OTHER SIDE OF THE BOUNDARY. Tabs work lands in the browser tree; socket work also
    # lands in the process holding the websocket, which no browser sampler can see. Without
    # this an arm can look cheap merely by moving its cost across the line being measured.
    client = os.path.join(REPO, ".fleet", "witness", "client_n23.csv")
    if os.path.exists(client):
        rows = [(float(r["ts"]), float(r["total_mb"]))
                for r in csv.DictReader(io.open(client, encoding="utf-8"))
                if r.get("total_mb")]
        during = [v for ts, v in rows if t_run0 <= ts <= t_run1]
        if during:
            out["client_peak_mb"] = round(max(during), 1)
            out["client_median_mb"] = round(st.median(during), 1)

    if "idle_start" in at and "idle_end" in at:
        idle = [(ts, v) for ts, v in trace if at["idle_start"] <= ts <= at["idle_end"]]
        if len(idle) >= 3:
            out["idle_start_mb"] = round(idle[0][1], 1)
            out["idle_end_mb"] = round(idle[-1][1], 1)
            out["decay_mb"] = round(idle[0][1] - idle[-1][1], 1)
            out["idle_s"] = round(idle[-1][0] - idle[0][0], 1)
    return out


def main(argv=None):                                            # pragma: no cover
    paths = sorted(glob.glob(os.path.join(DIAG, "*_events.json")))
    if not paths:
        print("no diagnostic runs under %s" % DIAG)
        return 1
    rows = [analyse(p) for p in paths]
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    return 0


if __name__ == "__main__":                                      # pragma: no cover
    sys.exit(main() or 0)
