"""Which archived A/B runs the current instrument produced, and which measure something else.

WHY THIS IS A MODULE AND NOT A JUDGEMENT MADE EACH TIME IT IS NEEDED

The results directory keeps every campaign, and the ones in it were NOT all taken with the
same instrument. Two changes landed on 2026-08-21 that altered what the number means:

  11:06  the memory sampler stopped reporting total RSS -- which tracked which arm ran first --
         and started reporting the commit growth each arm actually caused.
  12:33  the arms stopped sharing a memory store, so arm 2 stopped opening on arm 1's notes
         and reporting the cost of reading them as the cost of the work.

A run from before either one is a measurement of a different quantity. It is not "older data
worth less"; it is data about something else, and averaging it in is not conservatism.

This was worked out by hand once and the hand-made version got it wrong: the 300 MB floor in
`route_evaluator` was compared against a 130-180 MB spread that came entirely from PRE-isolation
nulls, and a treatment effect was inferred from runs that predate the sampler fix. Both errors
were the same error -- reading a number without asking which instrument produced it -- so the
rule now lives in code where it can be applied the same way twice.
"""
from __future__ import annotations

import glob
import itertools
import json
import math
import os

#: When the sampler began measuring the quantity the arm caused. Commit 1759570.
SAMPLER_EPOCH = 1787277967

#: When the arms became independent units. Commit 477705a.
SAMPLER_ISOLATION_EPOCH = 1787283181

#: THE THIRD CHANGE, AND THE ONE THE RECORDED FIELDS COULD NOT HAVE CAUGHT.
#:
#: Admission weighed a PENDING worker by `self.socket`, which attach() sets and admission reads
#: before attach runs -- so a worker about to take a socket and hold no tab was billed as a tab.
#: At a cap of 2 that made the fleet strictly serial on both routes. Every run before this stamp
#: recorded `max_concurrent: 2` and actually ran ONE worker at a time, so the field says the same
#: thing on both sides of the fix while the quantity underneath it changed. Filtering on the
#: recorded value alone would silently average the two.
#:
#: THE FOURTH CHANGE. The sampler summed every msedge.exe on the machine -- 45 processes and
#: 6,181 MB here, of which the fleet's Edge was 1,559 and 59% belonged to neither it nor the
#: bridge. Runs taken between the admission fix and this one cleared the previous stamp while
#: still measuring that population, so the stamp moves again rather than letting four nulls
#: measured on the whole machine pool with runs measured on the fleet's own browser.
#:
#: THE FIFTH CHANGE, found while planning the series rather than after it. The discarded
#: warm-up pass followed the CONTROL's transport, so a socket-vs-socket null warmed nothing
#: while a tabs-vs-socket treatment warmed Edge's renderer pool -- the two columns being
#: compared differed in whether renderer creation had already been paid for, and no null
#: flavour could price that difference. It is now a tab pass before EVERY arm.
#:
#: THE SIXTH CHANGE, and the one a pre-registered stopping rule caught rather than a reader.
#: Phase A's eight nulls put two identical arms 421 MB apart against a 300 MB floor, and the
#: cause was the STATISTIC, not the population: summing max(0, growth) per process turns
#: ordinary renderer churn into growth, because the process that exits is floored to zero while
#: its replacement is charged in full. Demonstrated with no arm running -- two idle minutes gave
#: 82.1 MB under the old statistic and 6.1 under a signed tree delta. The nine nulls measured
#: before this are void for setting any threshold.
#:
#: THE SEVENTH, and the diagnostic block that preceded the series caught it. The signed delta
#: fixed the tabs case outright -- tabs nulls came back +4.5 and -0.8 where generation 5 gave
#: +112 and -40 -- but socket arms open no tab, so the warm-up tab's teardown ran on unopposed
#: and every socket arm spent its whole length below its own baseline. The frozen judge starts
#: its peak at zero and only raises it, so those arms reported 0.0, and in a TREATMENT that
#: subtracts a clipped zero from a tabs arm's honest peak: socket wins by construction. The
#: baseline now waits for the browser to settle instead of for a guessed 1.6 seconds.
#:
#: THE EIGHTH, and this one nobody caught -- it was found by reading the archive, after five
#: nulls had already been pooled. The evaluation browser was launched with a window and opened
#: an intranet portal as its start page: a 273 MB page and a compositor surface, both inside the
#: process tree the sampler measures. Switching it to --headless=new with no start page removed
#: both. That is a change to the POPULATION, as surely as rescoping the sampler was, and it went
#: in without touching this number -- so four windowed nulls and one headless null sat in the
#: same basket claiming to be the same instrument. The four measured before the cutover are void.
#:
#: Worth recording plainly, because the epoch mechanism is only as good as the honesty of the
#: person moving it: discarding them costs data and buys nothing. Their spread was the widest in
#: the archive (-272.4, -140.8, +34.4, +11.7 on runs where the true answer is zero), and dropping
#: them leaves a single headless null. The reason to drop them is that they measured a different
#: browser, not that they were inconvenient.
#:
#: This is the epoch a run has to clear to be comparable with anything measured today.
#: 1787567132 -- the commit that made the evaluation browser headless.
INSTRUMENT_EPOCH = 1787567132

#: THE WORKLOAD IS PART OF THE INSTRUMENT TOO, AND ITS NAME DOES NOT CHANGE WHEN IT DOES.
#:
#: The `multiturn` set was run three times before it measured the work rather than its own
#: defects: goals that pointed at "the same folder" while naming no path (fixed in 960e474)
#: and a folder named by its 8.3 short form, which one arm spent three turns failing to write
#: into (fixed in 254d18a). Runs from before those carry the same `goals` string as runs from
#: after, so filtering on the name alone silently averages four numbers of which two describe
#: broken goals. A set earns an entry here the moment a run of it is superseded by a fix.
WORKLOAD_EPOCH = {"multiturn": 1787423013,
                  # The saturated set carried the SAME 8.3 short path through eight
                  # measured runs before anyone looked. One tabs arm spent 33.2 minutes
                  # on a single turn under it. Runs from before this stamp measured the
                  # set with that defect in it.
                  "saturated-v1": 1787431031}

RESULTS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                       "docs", "research", "results")


def _ts_of(experiment_id: str) -> int:
    """The run's clock, from the trailing stamp the campaign puts in the id."""
    tail = (experiment_id or "").rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def load(results_dir: str = None) -> list:
    """Every archived campaign, newest last, annotated with which instrument produced it."""
    d = results_dir or RESULTS
    out = []
    for path in sorted(glob.glob(os.path.join(d, "route_campaign_*.json"))):
        try:
            rec = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        exp = rec.get("ledger_experiment_id") or os.path.basename(path)
        ts = _ts_of(exp)
        out.append({
            "path": path, "experiment_id": exp, "ts": ts,
            "version": "v2" if "-v2-" in exp else "v1",
            "null": bool(rec.get("null_run")),
            # A run recorded before the field existed ran the set that was there then.
            "goals": rec.get("goals") or "saturated-v1",
            "arm_order": rec.get("arm_order") or "",
            "control_peak_mb": (rec.get("control") or {}).get("peak_mb"),
            "candidate_peak_mb": (rec.get("candidate") or {}).get("peak_mb"),
            "memory_gain_mb": rec.get("memory_gain_mb"),
            # Absent in runs recorded before the knob was written down; those all ran at 2/"1".
            "max_concurrent": rec.get("max_concurrent", 2),
            # Absent before the sampler was scoped; those runs measured every Edge on the box.
            "revision": str(rec.get("revision") or ""),
            # Absent before the evaluation browser existed; those runs all drove the fleet's.
            "cdp_url": str(rec.get("cdp_url") or "http://127.0.0.1:9222"),
            "memory_population": str((rec.get("control") or {}).get("memory_population")
                                     or "all-edge-unscoped"),
            "sidepage_reserve": str(rec.get("sidepage_reserve", "1")),
            # Absent before the flag was recorded; every run measured up to that point used it.
            "warmup": bool(rec.get("warmup", True)),
            "current_instrument": ts >= INSTRUMENT_EPOCH,
        })
    out.sort(key=lambda r: r["ts"])
    return out


def comparable(runs, *, goals: str, version: str = "v1", null: bool = None,
               max_concurrent: int = 2, sidepage_reserve: str = "1",
               memory_population: str = "fleet-edge-tree",
               cdp_url: str = "http://127.0.0.1:9222", warmup: bool = True) -> list:
    """The runs that can be put in one column together.

    Filters on the instrument AND on the goal set AND on the version of that goal set AND on
    the candidate version. A difference measured on one goal set is not evidence about another
    -- the two sets here have different spreads -- v1 and v2 are different candidates, a goal
    set keeps its name across the fixes that change what it measures, HOW MANY WORKERS RUN AT
    ONCE changes how much memory a run costs, and WHICH BROWSERS WERE COUNTED changes what the
    number is about at all.

    WHICH BROWSER is on the list for the same reason. The fleet's Edge carries a resident page
    that belongs to no arm and moved by up to 239 MB during them; a dedicated evaluation
    browser does not. Those are different measurements, not the same one taken twice.

    That last one is why recording it was not enough. The sampler falls back to summing every
    Edge on the machine when it cannot resolve the CDP port owner, and it says so in the
    result -- but a field nobody filters on is a field that lets the two sit in one column.

    WHETHER THE ARMS WERE WARMED is the newest axis and was added before it was needed rather
    than after. The warm-up drives the tabs route before every arm, so the baseline already
    holds a renderer that the tabs arm reuses free and the socket arm lets decay; two reviewers
    read that bias in opposite directions, and the diagnostic that settles it runs without the
    warm-up. Those runs land in this same archive. Without this filter they would pool with the
    series exactly as four windowed nulls pooled with a headless one earlier the same night --
    the same defect, caught the second time before it cost anything rather than after.
    """
    since = WORKLOAD_EPOCH.get(goals, 0)
    sel = [r for r in runs
           if r["current_instrument"] and r["goals"] == goals and r["version"] == version
           and r["ts"] >= since
           and r["max_concurrent"] == max_concurrent
           and r["warmup"] == warmup
           and r["sidepage_reserve"] == str(sidepage_reserve)
           and r["memory_population"] == str(memory_population)
           and r["cdp_url"] == str(cdp_url)]
    if null is not None:
        sel = [r for r in sel if r["null"] is bool(null)]
    return sel


def spread(runs) -> dict:
    """Summary of a column of runs. `None` where there is nothing to report.

    Deliberately NOT a verdict. The verdict lives in `route_evaluator.decide`, which is frozen
    and takes one pair; this only says what the archive contains, so a floor can be re-derived
    from something reproducible instead of from whichever runs were remembered.
    """
    gains = [r["memory_gain_mb"] for r in runs if r["memory_gain_mb"] is not None]
    if not gains:
        return {"n": 0, "min": None, "max": None, "mean": None, "widest": None}
    return {"n": len(gains), "min": min(gains), "max": max(gains),
            "mean": round(sum(gains) / len(gains), 1),
            "widest": round(max(gains) - min(gains), 1)}


def separation(null_gains, treatment_gains) -> dict:
    """How surprising the treatment column would be if the label meant nothing.

    An EXACT one-sided permutation test, not an approximation: pool the two columns, enumerate
    every way of splitting the pool into groups of the observed sizes, and count how often the
    difference in means is at least as large as the one observed. With this many runs the exact
    enumeration is cheap and a normal approximation would be wrong.

    ONE-SIDED IS LEGITIMATE HERE ONLY BECAUSE THE DIRECTION WAS FIXED FIRST. The hypothesis is
    that a socket uses LESS memory, recorded before any of these runs; `memory_gain_mb` is
    control minus candidate, so the prediction is that treatment gains sit ABOVE null gains. A
    direction chosen after seeing the numbers would halve the p-value for free.

    `min_p` is the smallest p this many runs can produce, and it is reported every time: with
    two against two the answer cannot go below 0.167 no matter how cleanly the columns
    separate, and a p that has hit its own floor is a statement about the sample size.

    Both columns must come from `comparable()` with the SAME goal set and version. This takes
    plain lists and cannot check that, so the caller carries it.
    """
    a, b = list(null_gains), list(treatment_gains)
    if not a or not b:
        return {"p": None, "min_p": None, "n_null": len(a), "n_treatment": len(b),
                "observed": None, "why": "a column is empty"}
    pool = a + b
    n = len(a)
    observed = sum(b) / len(b) - sum(a) / len(a)
    total = at_least = 0
    for combo in itertools.combinations(range(len(pool)), n):
        left = [pool[i] for i in combo]
        right = [pool[i] for i in range(len(pool)) if i not in set(combo)]
        diff = sum(right) / len(right) - sum(left) / len(left)
        total += 1
        if diff >= observed - 1e-9:
            at_least += 1
    return {"p": round(at_least / total, 4),
            "min_p": round(1.0 / math.comb(len(pool), n), 4),
            "n_null": len(a), "n_treatment": len(b),
            "observed": round(observed, 1)}


def revisions(runs) -> list:
    """The distinct code revisions a column spans, newest last.

    NOT a filter. A series that spans a change to the route, the fleet or the sampler is not
    one series, but which changes matter is a judgement -- a docstring edit is not a new
    instrument and an admission rule is. So this reports the span and a human decides, rather
    than silently splitting columns on every commit.
    """
    out = []
    for r in runs:
        rev = r.get("revision") or ""
        if rev and rev not in out:
            out.append(rev)
    return out


def report(results_dir: str = None) -> str:
    """A table of what the archive holds, for a human deciding whether a floor can move."""
    runs = load(results_dir)
    lines = ["archive: %d runs, %d from the current instrument"
             % (len(runs), sum(1 for r in runs if r["current_instrument"]))]
    for goals in sorted({r["goals"] for r in runs}):
        for version in sorted({r["version"] for r in runs}):
            for null in (True, False):
                sel = comparable(runs, goals=goals, version=version, null=null)
                if not sel:
                    continue
                s = spread(sel)
                lines.append("  %-13s %-3s %-9s n=%d  gains %s  spread %s MB"
                             % (goals, version, "null" if null else "treatment", s["n"],
                                [r["memory_gain_mb"] for r in sel], s["widest"]))
    for goals in sorted({r["goals"] for r in runs}):
        nulls = [r["memory_gain_mb"] for r in comparable(runs, goals=goals, null=True)]
        txs = [r["memory_gain_mb"] for r in comparable(runs, goals=goals, null=False)]
        if nulls and txs:
            sep = separation(nulls, txs)
            lines.append("  %-13s treatment mean sits %s MB above null, p=%s "
                         "(the smallest p this many runs can give: %s)"
                         % (goals, sep["observed"], sep["p"], sep["min_p"]))
    return "\n".join(lines)


if __name__ == "__main__":                                      # pragma: no cover
    print(report())
