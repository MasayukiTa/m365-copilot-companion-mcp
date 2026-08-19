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
import os
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


#: A lens session that ran its whole budget produced no opinion. Rows carrying one cannot be
#: scored: the policy that would have run that lens gets credit for a clean cell it never got.
LENS_TIMEOUT_S = 420.0


def drop_incomplete(rows, *, timeout_s=LENS_TIMEOUT_S):
    """(usable rows, dropped rows). Applied whoever wrote the corpus.

    The collector refuses these at collection time, but an older corpus predates that check
    and a hand-assembled one never had it. The filter lives here too so the analysis cannot
    be handed a holed row by a file.
    """
    keep, drop = [], []
    for row in rows:
        detail = row.get("lens_detail") or {}
        starved = [lens for lens, d in detail.items()
                   if d.get("verdict") == A.UNCLEAR
                   and float(d.get("elapsed_s") or 0) >= timeout_s * 0.95]
        (drop if starved else keep).append(row)
    return keep, drop


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



def measured_lens_cost(rows):
    """Median seconds per lens, from the corpus itself. None when nothing usable was timed.

    ASSUMING EQUAL COST IS A MODELLING CHOICE, not a neutral default: the frontier trades
    false accepts against review spend, so treating a slow lens as costing the same as a fast
    one silently favours running it. Timed-out lenses are excluded -- their elapsed time is
    the timeout, which measures the harness rather than the lens.
    """
    per = {}
    for row in rows:
        for lens, d in (row.get("lens_detail") or {}).items():
            secs = d.get("elapsed_s")
            if secs is None or d.get("verdict") == A.UNCLEAR:
                continue
            per.setdefault(lens, []).append(float(secs))
    if not per:
        return None
    out = {}
    for lens, xs in per.items():
        xs = sorted(xs)
        mid = len(xs) // 2
        out[lens] = xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2.0
    return out


def warm_memory(rows, path):
    """A memory trained on `rows` only. Returns (memory, observations).

    The point of the split. `simulate` already refuses a cold memory, because a cold adaptive
    policy returns the panel's own order and is the fixed policy under another name. A memory
    warmed on the SAME rows it is then scored on is the opposite error and a quieter one: the
    adaptive arm is read on data it has already seen, and it wins for that reason.
    """
    from relay.refuter_memory import RefuterMemory

    # A FRESH STORE EVERY TIME. RefuterMemory appends, so re-running the analysis against the
    # same path warms it twice -- observed: 54 observations became 108 on the second run, off
    # the same corpus. The adaptive arm would then look better the more often the analysis was
    # re-run, which is the least defensible way to move a number.
    if os.path.exists(str(path)):
        os.remove(str(path))
    memory = RefuterMemory(path=str(path))
    for row in rows:
        features = row.get("features") or {}
        for lens, verdict in row["verdicts"].items():
            memory.record(features, lens, refuted=(verdict == A.REFUTED))
    return memory, A.memory_observations(memory)


def split(rows, *, fraction=0.5):
    """Train/test by candidate, deterministic and STRATIFIED by the label.

    A plain id-order cut is not neutral. Run on the corpus this was built against, it put
    every bad candidate on the train side and left the test half with two catchable failures
    -- so the held-out view refused, correctly, and the split was the only reason. Bad
    candidates are the scarce thing here; both halves need their share of them.
    """
    def strat(row):
        functional, security = A._truth(row)
        return (bool(functional), security)

    buckets = {}
    for row in sorted(rows, key=lambda r: str(r.get("candidate_id"))):
        buckets.setdefault(strat(row), []).append(row)
    train, test = [], []
    for _key, group in sorted(buckets.items(), key=lambda kv: str(kv[0])):
        cut = int(len(group) * fraction)
        train.extend(group[:cut])
        test.extend(group[cut:])
    return train, test


def run(rows, *, k, memory=None, label="", lens_cost=None, held_out=False):
    results = []
    for policy in A.POLICIES:
        try:
            results.append(A.simulate(rows, policy, k=k, memory=memory,
                                      lens_cost=lens_cost))
        except A.AllocationError as exc:
            # A POLICY THAT COULD NOT BE SCORED IS NOT A POLICY THAT SCORED BADLY. Dropping it
            # silently would leave a frontier that looks complete and is missing an arm.
            print("  %-9s REFUSED: %s" % (policy, exc))
    front = A.frontier(results)
    print()
    # COST IS SHOWN BECAUSE IT WAS MEASURED. Printing calls alone would leave the measured
    # per-lens seconds doing no work, and "calls" is the column that treats a slow lens and a
    # fast one as the same expense.
    print("  %-9s %8s %8s %10s %8s %10s" % ("policy", "FA(all)", "FA(catch)", "FA(sec)",
                                            "calls", "cost(s)"))
    for r in sorted(results, key=lambda r: r["policy"]):
        print("  %-9s %8d %8d %10d %8d %10s"
              % (r["policy"], r["false_accept"], r["false_accept_catchable"],
                 r["false_accept_security"], r["review_calls"],
                 ("%.0f" % r["cost"]) if lens_cost else "-"))
    print()
    print("  frontier: %s" % (", ".join(front["frontier"]) or "(none)"))
    print("  %s" % front.get("note", ""))
    # THE CAVEATS ARE CORRECTED FOR THIS VIEW, not printed verbatim. `frontier` cannot know
    # how it was called, so it warns about train/test contamination unconditionally -- true of
    # every other view here and FALSE of this one. Printing a disclaimer that does not apply
    # teaches the reader to skip all of them.
    for line in front.get("does_not_support") or []:
        if held_out and line.startswith(("train/test separation", "generalisation")):
            continue
        print("    does not support -- %s" % line)
    if held_out:
        print("    DOES support -- train/test separation: the memory was warmed on the other "
              "half of the corpus and this arm never saw these candidates")
        print("    still does not support -- generalisation beyond this corpus: the held-out "
              "half is drawn from the same pools and the same agent")
    return {"label": label, "results": results, "frontier": front}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus")
    ap.add_argument("--k", type=int, default=2)
    ap.add_argument("--json-out", default="")
    ap.add_argument("--split", default="",
                    help="path for a memory warmed on half the corpus; the adaptive arm is "
                         "then scored only on the half it never saw")
    args = ap.parse_args()

    rows = load(args.corpus)
    if not rows:
        print("the corpus is empty. A collector that skips every candidate produces exactly "
              "this file, and it reads the same as a clean run -- check the collector's "
              "skipped list before treating this as a result.")
        return 2

    rows, holed = drop_incomplete(rows)
    if holed:
        print("dropped %d row(s) whose panel had a hole: at least one lens ran out the clock "
              "rather than answering, and scoring a policy that would have run it against an "
              "empty cell credits it with a result it never got" % len(holed))
    if not rows:
        print("nothing usable is left. Every row had a silent lens -- that is a harness "
              "result, not a measurement of the policies.")
        return 2
    shape = describe(rows)
    print("CORPUS: %(candidates)d candidates | bad: %(functional_bad)d functional, "
          "%(security_bad)d security | unevaluable: %(security_unevaluable)d | "
          "catchable: %(catchable_bad)d | seeded: %(calibration_rows)d" % shape)

    cost = measured_lens_cost(rows)
    if cost:
        print("measured lens cost (median s, timed-out lenses excluded): %s"
              % ", ".join("%s=%.0f" % (k2, v) for k2, v in sorted(cost.items())))
    else:
        print("no usable lens timings in this corpus; every lens is treated as equally "
              "expensive, which is a modelling choice and not a measurement")

    out = {"shape": shape, "views": [], "lens_cost": cost}
    # BOTH VIEWS, ALWAYS. The seeded rows are what gives the security axis a denominator, and
    # they are also candidates no real agent produced. Reporting only the combined view would
    # overstate what was observed; reporting only the real one would hide that the security
    # comparison rests entirely on seeds.
    print()
    print("[all rows, seeded included]")
    out["views"].append(run(rows, k=args.k, label="all", lens_cost=cost))
    real = [r for r in rows if not r.get(CALIBRATION_KEY)]
    if len(real) != len(rows):
        print()
        print("[real candidates only, %d rows]" % len(real))
        out["views"].append(run(real, k=args.k, label="real_only", lens_cost=cost))

    # HELD OUT, because an adaptive arm warmed on the rows it is then scored on wins for a
    # reason that has nothing to do with being adaptive.
    if args.split:
        train, test = split(rows)
        memory, seen = warm_memory(train, Path(args.split))
        print()
        print("[held out: memory warmed on %d candidates, scored on the other %d "
              "-- %d observations]" % (len(train), len(test), seen))
        out["views"].append(run(test, k=args.k, memory=memory, label="held_out",
                                lens_cost=cost, held_out=True))

    if args.json_out:
        io.open(args.json_out, "w", encoding="utf-8", newline=chr(10)).write(
            json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        print()
        print("written to %s" % args.json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
