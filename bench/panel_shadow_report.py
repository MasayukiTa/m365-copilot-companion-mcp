"""What a veto aggregator WOULD have cost, counted from panels that already ran.

The decision this file exists to inform: `aggregate_panel` takes a strict majority, which
lets a lone security finding be voted down by two lenses that never examined it. A veto fixes
that and introduces its own risk -- if the security lens is noisy, every false alarm becomes a
work stoppage. Nobody can weigh those against each other from an opinion about the lenses.

So the panel records its per-lens verdicts and the counterfactual verdict, and this reads them
back. NO SECOND FLEET RUN IS NEEDED: every ultra panel already runs a security reviewer, and
the flips are computable from the runs that produced them.

WHAT THE NUMBERS DO AND DO NOT SAY. `flip_rate` is how often the two aggregators disagree --
an upper bound on the veto's cost, not the cost itself, because a flip on a REAL defect is a
benefit. Separating those needs a human to read `reasons` and say which findings were real.
This file will not guess that, and prints the reasons rather than a verdict.

Usage:
  python -m bench.panel_shadow_report                     # .fleet/panels.jsonl
  python -m bench.panel_shadow_report --ledger <path> --show 20
"""
from __future__ import annotations

import argparse
import collections
import json
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LEDGER = os.path.join(REPO, ".fleet", "panels.jsonl")


def read_ledger(path):
    """Every panel line the ledger holds. A malformed line is skipped, not fatal -- the file
    is appended to by a live run and its last line can be half-written."""
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("kind") == "panel":
                    rows.append(row)
    except OSError:
        return []
    return rows


def summarize(rows):
    """Counts, per-lens refusal rates, and the flips -- the three things the decision needs."""
    rows = list(rows or [])
    flips = [r for r in rows if r.get("would_flip")]
    per_lens = collections.defaultdict(lambda: collections.Counter())
    faulted = 0
    for r in rows:
        if r.get("harness_faults"):
            faulted += 1
        for l in r.get("lenses") or []:
            per_lens[l.get("lens") or "?"][l.get("kind") or "?"] += 1

    lens_rates = {}
    for lens, c in per_lens.items():
        # DENOMINATOR EXCLUDES UNCLEAR ON PURPOSE. A reviewer that could not decide has not
        # said the work is fine, and counting it as a non-refusal would credit the lens with
        # a clean look it never took.
        answered = c["REFUTED"] + c["UPHELD"]
        lens_rates[lens] = {
            "refuted": c["REFUTED"], "upheld": c["UPHELD"], "unclear": c["UNCLEAR"],
            "refute_rate": (c["REFUTED"] / answered) if answered else None,
        }

    return {
        "panels": len(rows),
        "flips": len(flips),
        "flip_rate": (len(flips) / len(rows)) if rows else None,
        "panels_with_a_harness_fault": faulted,
        "per_lens": lens_rates,
        "flip_rows": flips,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", default=DEFAULT_LEDGER)
    ap.add_argument("--show", type=int, default=10, help="how many flip reasons to print")
    a = ap.parse_args(argv)

    rows = read_ledger(a.ledger)
    s = summarize(rows)
    print("=== panel veto shadow: %s ===" % a.ledger)
    if not s["panels"]:
        # SAYS NOTHING RATHER THAN SAYING ZERO. An unread ledger and a veto that never fired
        # produce the same "0 flips", and only one of them is a result.
        print("  no panel records yet -- run a panel (--effort ultra / --panel) first.")
        print("  NOTE: this is 'nothing measured', NOT 'the veto would never have fired'.")
        return 0
    print("  panels        : %d" % s["panels"])
    print("  would flip    : %d  (%.1f%% of panels)" % (s["flips"], 100 * s["flip_rate"]))
    print("  harness faults: %d panel(s) had a lens that could not be conducted"
          % s["panels_with_a_harness_fault"])
    for lens, r in sorted(s["per_lens"].items()):
        rate = "n/a" if r["refute_rate"] is None else "%.1f%%" % (100 * r["refute_rate"])
        print("  lens %-12s refuted %3d / upheld %3d / unclear %3d  (refute rate %s)"
              % (lens, r["refuted"], r["upheld"], r["unclear"], rate))
    if s["flips"]:
        print("\n  the flips -- each is a finding a human must judge REAL or NOISE:")
        for r in s["flip_rows"][:a.show]:
            print("   - %s" % (r.get("veto_shadow") or {}).get("reason", "")[:200])
        if s["flips"] > a.show:
            print("   ... and %d more" % (s["flips"] - a.show))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
