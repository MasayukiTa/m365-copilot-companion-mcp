"""Read an all-lenses corpus and report the section 18 frontier, or why there isn't one.

Separate from the collector on purpose. Collection is a live run measured in hours; the
analysis has to be re-runnable in a second, against the same rows, when a question about how
they were read comes up later.

WHAT THIS PRINTS IS THE MODULE'S OWN REFUSALS. It does not summarise them away. If the corpus
cannot support a frontier -- fewer than five bad candidates any lens would have caught -- the
refusal is the output, because "here is a frontier over three events" is the failure this
whole section was built to avoid.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bench.companionbench.calibration import CALIBRATION_KEY   # noqa: E402
from relay.selfimprove import reviewer_allocation as A         # noqa: E402


def load(path):
    rows = []
    for line in io.open(path, encoding="utf-8"):
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def describe(rows) -> dict:
    """What the corpus contains, before any policy is scored against it."""
    bad_f = bad_s = unev = 0
    for row in rows:
        functional, security = A._truth(row)
        if not functional:
            bad_f += 1
        if security == A.SECURITY_VIOLATION:
            bad_s += 1
        elif security == A.SECURITY_UNEVALUABLE:
            unev += 1
    catchable = sum(1 for row in rows
                    if (not A._truth(row)[0] or A._truth(row)[1] == A.SECURITY_VIOLATION)
                    and any(v == A.REFUTED for v in row["verdicts"].values()))
    return {"candidates": len(rows), "functional_bad": bad_f, "security_bad": bad_s,
            "security_unevaluable": unev, "catchable_bad": catchable,
            "calibration_rows": sum(1 for r in rows if r.get(CALIBRATION_KEY))}


def run(rows, *, k, memory=None, label=""):
    results = []
    for policy in A.POLICIES:
        try:
            results.append(A.simulate(rows, policy, k=k, memory=memory))
        except A.AllocationError as exc:
            # A POLICY THAT COULD NOT BE SCORED IS NOT A POLICY THAT SCORED BADLY. Dropping it
            # silently would leave a frontier that looks complete and is missing an arm.
            print("  %-9s REFUSED: %s" % (policy, exc))
    front = A.frontier(results)
    print()
    print("  %-9s %8s %8s %10s %8s" % ("policy", "FA(all)", "FA(catch)", "FA(sec)", "calls"))
    for r in sorted(results, key=lambda r: r["policy"]):
        print("  %-9s %8d %8d %10d %8d"
              % (r["policy"], r["false_accept"], r["false_accept_catchable"],
                 r["false_accept_security"], r["review_calls"]))
    print()
    print("  frontier: %s" % (", ".join(front["frontier"]) or "(none)"))
    print("  %s" % front.get("note", ""))
    for line in front.get("does_not_support") or []:
        print("    does not support -- %s" % line)
    return {"label": label, "results": results, "frontier": front}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus")
    ap.add_argument("--k", type=int, default=2)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    rows = load(args.corpus)
    if not rows:
        print("the corpus is empty. A collector that skips every candidate produces exactly "
              "this file, and it reads the same as a clean run -- check the collector's "
              "skipped list before treating this as a result.")
        return 2

    shape = describe(rows)
    print("CORPUS: %(candidates)d candidates | bad: %(functional_bad)d functional, "
          "%(security_bad)d security | unevaluable: %(security_unevaluable)d | "
          "catchable: %(catchable_bad)d | seeded: %(calibration_rows)d" % shape)

    out = {"shape": shape, "views": []}
    # BOTH VIEWS, ALWAYS. The seeded rows are what gives the security axis a denominator, and
    # they are also candidates no real agent produced. Reporting only the combined view would
    # overstate what was observed; reporting only the real one would hide that the security
    # comparison rests entirely on seeds.
    print()
    print("[all rows, seeded included]")
    out["views"].append(run(rows, k=args.k, label="all"))
    real = [r for r in rows if not r.get(CALIBRATION_KEY)]
    if len(real) != len(rows):
        print()
        print("[real candidates only, %d rows]" % len(real))
        out["views"].append(run(real, k=args.k, label="real_only"))

    if args.json_out:
        io.open(args.json_out, "w", encoding="utf-8", newline=chr(10)).write(
            json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        print()
        print("written to %s" % args.json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
