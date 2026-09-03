# -*- coding: utf-8 -*-
"""The baseline run's result, stated with the cases that are genuinely arguable shown both ways.

WHY THIS IS NOT JUST pro_ledger_report. That reader answers "what does the ledger say", which is
the right question for the ledger and the wrong one for a benchmark claim. Three instances in
this run produced something, and whether that something counts against the model is a judgement
call rather than a fact:

  oversize        the worker emitted a 4 MB diff and capture refused it as "not a fix". It
                  produced output, so this is not the harness failing -- but the capture takes
                  the diff including anything a build regenerated, so it is not purely the
                  model's doing either.
  stuck-no-patch  the worker was declared STUCK after ten retries and wrote nothing. Fourteen
                  turns without progress is arguably a failure; a transport that would not
                  settle is arguably not.
  interrupted     a websocket dropped mid-turn. A patch was produced and graded, so it is
                  already in the rate -- but it came from one turn, not from a finished attempt.

Reporting one number silently picks an answer to all three. This prints the rate with them
excluded and with them charged, so the reader can see how much the choice is worth. If the two
numbers disagree by more than the run's own noise, neither should be quoted alone.
"""
from __future__ import annotations

import argparse
import json
import math
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SW = os.path.join(REPO, ".fleet", "swe")

from bench import pro_ledger_report as PR   # noqa: E402


def wilson(k, n, z=1.96):
    """A confidence interval that stays inside [0,1] at small n, unlike the normal one.

    At n=40 the textbook interval can reach past 1.0 and invites "up to 80%" readings of a
    result that supports no such thing.
    """
    if not n:
        return (0.0, 1.0)
    p = k / float(n)
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def load_arguable(path):
    """{instance_id: reason}. Absent file means none were recorded, not that none exist."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bench.pro_baseline_report",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--slice", default=os.path.join(SW, "pro_slice_baseline_20260903.json"))
    ap.add_argument("--ledger", default=os.path.join(SW, "pro_cycle_results.json"))
    ap.add_argument("--arguable", default=os.path.join(SW, "pro_arguable.json"),
                    help="{instance_id: why} for instances whose exclusion is a judgement call")
    ap.add_argument("--gold", default=os.path.join(SW, "gold_results.json"),
                    help="verdicts from grading the dataset's OWN gold patch. An instance gold "
                         "cannot solve is not a question the model was asked; leaving it in the "
                         "denominator charges the model for a broken instance")
    a = ap.parse_args(argv)

    want = PR._slice_ids(a.slice)
    if want is None:
        print("could not read the slice at %s" % a.slice)
        return 1
    rows = {i: r for i, r in PR.latest_rows(a.ledger).items() if i in want}
    arguable = load_arguable(a.arguable)

    # GOLD IS THE CALIBRATION. The grade is computed as (f2p | p2p) <= passed_tests, where
    # passed_tests comes from a parser reading stdout -- so a name the parser cannot normalise
    # makes a passing test invisible and the instance unresolvable by anyone. The dataset's own
    # gold patch is the one input that must resolve; where it does not, the instance is broken
    # and the model was never really asked the question.
    #
    # Measured 2026-09-03: gold resolved 39 of 40. The one it could not is excluded here and
    # named below, never silently dropped.
    unsolvable = {}
    gold = PR.latest_rows(a.gold) if a.gold else {}
    for inst, row in gold.items():
        if inst in rows and str(row.get("verdict") or "").upper() != "RESOLVED":
            unsolvable[inst] = "gold パッチでも解決しない"
    if unsolvable:
        rows = {i: r for i, r in rows.items() if i not in unsolvable}

    t = PR.tally(rows)
    k, n = len(t["resolved"]), t["evaluated"]
    lo, hi = wilson(k, n)
    if unsolvable:
        print("EXCLUDED AS BROKEN -- the dataset's gold patch does not resolve these either:")
        for i in sorted(unsolvable):
            print("    %s" % i[:74])
        print()

    print("population: %s -- %d of %d instance(s) have a row"
          % (os.path.basename(a.slice), len(rows), len(want)))
    print()
    print("AS MEASURED (the arguable cases excluded)")
    print("  RESOLVED %d / %d = %.1f%%   95%% CI %.1f-%.1f%%"
          % (k, n, 100.0 * k / max(1, n), 100 * lo, 100 * hi))

    charged = [i for i in arguable if i in want and i not in rows]
    if charged:
        n2 = n + len(charged)
        lo2, hi2 = wilson(k, n2)
        print()
        print("IF THE ARGUABLE CASES ARE CHARGED AS FAILURES")
        print("  RESOLVED %d / %d = %.1f%%   95%% CI %.1f-%.1f%%"
              % (k, n2, 100.0 * k / max(1, n2), 100 * lo2, 100 * hi2))
        print()
        print("  the %d instance(s) that moves:" % len(charged))
        for i in sorted(charged):
            print("    %-64s %s" % (i[:64], arguable[i]))
        print()
        print("  the choice is worth %.1f percentage points"
              % (100.0 * k / max(1, n) - 100.0 * k / max(1, n2)))
    else:
        print()
        print("no arguable cases recorded (%s)" % a.arguable)

    missing = sorted(i for i in want if i not in rows and i not in arguable)
    if missing:
        print()
        print("NEITHER MEASURED NOR ARGUED -- %d instance(s) simply have no result:" % len(missing))
        for i in missing[:6]:
            print("    %s" % i[:70])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
