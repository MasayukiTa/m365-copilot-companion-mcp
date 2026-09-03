# -*- coding: utf-8 -*-
"""Read the Pro verdict ledger and state the result WITH the things excluded from it.

WHY THIS IS A FILE AND NOT A ONE-LINER. Nothing read this ledger for reporting, so every number
ever quoted from it -- "77 scored, 39 resolved, 50.6%" among them -- was computed ad hoc at a
prompt. Each of those computations re-decided, silently and differently, what counts as scored.
That is not a reporting inconvenience: the resolve rate IS the denominator decision, and a
benchmark whose denominator is chosen fresh each time it is quoted has no result.

THE THREE POPULATIONS, WHICH ARE NOT INTERCHANGEABLE:

  RESOLVED / not   an evaluation ran and the patch either fixed the bug or did not. Only these
                   two belong in the rate.

  EVALERR          nothing was evaluated. No image, no container, a read-only filesystem. The
                   harness coerces these to false before writing eval_results.json, so they
                   reach the ledger looking exactly like a wrong patch -- fourteen of them in 87
                   seconds once, read as 0.0%. Recovered from the harness log; see
                   pro_grade_remote.unevaluated_instances.

  NOPATCH          the worker produced nothing, so there was nothing to evaluate. Recorded as
                   "not" until today, which both counted it against the model and retired the
                   instance. Nineteen of these came from one run where the fleet gate was
                   refusing and no worker ever started.

Neither of the last two is a statement about the model, and neither may share a denominator with
the first. They are printed anyway, and loudly, because an instance that silently leaves the
denominator is the other way to get an unreadable number.

    python -m bench.pro_ledger_report
    python -m bench.pro_ledger_report --by-repo
"""
from __future__ import annotations

import argparse
import json
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.join(REPO, ".fleet", "swe", "pro_cycle_results.json")

#: Verdicts that mean no evaluation happened. Kept in step with pro_cycle._NOT_A_GRADE by a
#: test rather than by memory -- two copies of a rule about the same file is how they drift.
NOT_A_MEASUREMENT = {"EVALERR", "NOPATCH", ""}

_INSTANCE = re.compile(r"^instance_(?P<owner>.+?)__(?P<repo>.+?)-[0-9a-f]{40}")


def repo_of(instance_id: str) -> str:
    """The repository an instance belongs to, or "?" when the id does not carry one.

    Not guessed from a prefix: ids look like instance_<owner>__<repo>-<sha>[-v<sha>], and both
    owner and repo may contain hyphens (future-architect__vuls), so the 40-hex commit is the only
    reliable place to stop.
    """
    m = _INSTANCE.match(instance_id or "")
    return m.group("repo") if m else "?"


def latest_rows(path: str = None) -> dict:
    """{instance_id: row} keeping the LAST row about each instance.

    The ledger is append-only, so a later row is a correction of an earlier one -- that is how a
    verdict written while the eval host was read-only gets retracted. Reading it any other way
    makes a false measurement permanent, which it was for sixteen instances.
    """
    out = {}
    try:
        with open(path or LEDGER, encoding="utf-8-sig") as fh:
            text = fh.read()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("instance_id"):
            out[row["instance_id"]] = row
    return out


def tally(rows: dict) -> dict:
    """Split the ledger into the three populations. Returns counts plus the ids of each."""
    resolved, failed, unevaluated, nopatch, other = [], [], [], [], []
    for inst, row in rows.items():
        v = str(row.get("verdict") or "").upper()
        if v == "RESOLVED":
            resolved.append(inst)
        elif v == "NOT":
            failed.append(inst)
        elif v == "EVALERR":
            unevaluated.append(inst)
        elif v == "NOPATCH":
            nopatch.append(inst)
        else:
            other.append(inst)
    evaluated = len(resolved) + len(failed)
    return {
        "resolved": sorted(resolved), "failed": sorted(failed),
        "unevaluated": sorted(unevaluated), "nopatch": sorted(nopatch),
        "other": sorted(other),
        "evaluated": evaluated,
        # None, NOT zero. A rate over an empty denominator is not 0.0%, and printing it as one
        # has already turned "nothing was measured" into "everything failed" in this pipeline.
        "rate": (len(resolved) / float(evaluated)) if evaluated else None,
    }


def format_report(t: dict, by_repo=False, rows=None) -> str:
    out = []
    rate = "n/a (nothing was evaluated)" if t["rate"] is None else "%.1f%%" % (100 * t["rate"])
    out.append("RESOLVED %d / %d evaluated = %s" % (len(t["resolved"]), t["evaluated"], rate))
    excluded = len(t["unevaluated"]) + len(t["nopatch"]) + len(t["other"])
    if excluded:
        out.append("")
        out.append("EXCLUDED from that rate -- neither is a statement about the model:")
        if t["unevaluated"]:
            out.append("  %3d never evaluated (EVALERR: no image, nothing ran)"
                       % len(t["unevaluated"]))
        if t["nopatch"]:
            out.append("  %3d produced no patch (NOPATCH: nothing to evaluate)"
                       % len(t["nopatch"]))
        if t["other"]:
            out.append("  %3d carry a verdict this reader does not recognise: %s"
                       % (len(t["other"]),
                          ", ".join(sorted({str((rows or {}).get(i, {}).get("verdict"))
                                            for i in t["other"]})[:4])))
        out.append("  %3d instances in total are in the ledger but not in the rate" % excluded)
    if by_repo:
        buckets = {}
        for inst in t["resolved"]:
            buckets.setdefault(repo_of(inst), [0, 0])[0] += 1
        for inst in t["resolved"] + t["failed"]:
            buckets.setdefault(repo_of(inst), [0, 0])[1] += 1
        out.append("")
        out.append("by repository (evaluated only):")
        for name in sorted(buckets, key=lambda k: (-buckets[k][1], k)):
            got, tot = buckets[name]
            out.append("  %-28s %2d/%2d  %5.1f%%" % (name, got, tot, 100.0 * got / max(1, tot)))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bench.pro_ledger_report",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--ledger", default=LEDGER)
    ap.add_argument("--by-repo", action="store_true")
    a = ap.parse_args(argv)
    rows = latest_rows(a.ledger)
    if not rows:
        print("no ledger at %s" % a.ledger)
        return 1
    t = tally(rows)
    print(format_report(t, a.by_repo, rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
