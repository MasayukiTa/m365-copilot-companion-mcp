"""Read the per-attempt snapshots and answer the question a single final patch cannot.

WHY THEY EXIST. Only the final patch per instance was ever kept, so every attempt of a goal
joined to the same grader verdict: the graded retry floor came out flat by construction, k=1
and k=2 identical, and had to refuse rather than report it. Two independent reviews named that
as the blocker on measuring the one mechanism in this system with a signal.

WHAT THEY UNLOCK, once the snapshots are graded:

    rescue      P(correct at attempt 2 | wrong at attempt 1)   -- retry earning its keep
    regression  P(wrong at attempt 2  | correct at attempt 1)  -- retry destroying an answer

The second is the one nobody looks for. A retry policy that rescues 30% and breaks 20% is not
a 30% improvement, and the completion floor cannot see the difference at all: both attempts
report DONE.
"""
from __future__ import annotations

import glob
import io
import json
import os
from collections import defaultdict

SNAP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ".fleet", "swe", "attempts")


def load(directory=SNAP_DIR):
    """instance_id -> [snapshot, ...] in capture order."""
    out = defaultdict(list)
    for p in sorted(glob.glob(os.path.join(directory, "*.json"))):
        try:
            d = json.load(io.open(p, encoding="utf-8-sig"))
        except ValueError:
            continue
        if d.get("instance_id"):
            out[d["instance_id"]].append(d)
    for v in out.values():
        v.sort(key=lambda x: x.get("captured_at") or 0)
    return dict(out)


def distinct_attempts(snapshots):
    """How many instances actually produced DIFFERENT patches across attempts.

    Identical patches across attempts are not two attempts for this purpose: grading them
    twice answers nothing, and counting them as two would inflate the denominator of any
    rescue rate.
    """
    n = 0
    for rows in snapshots.values():
        if len({r.get("patch_sha256_16") for r in rows}) > 1:
            n += 1
    return n


def transitions(snapshots, verdict_by_hash):
    """rescue / regression / stable counts, over instances whose attempts can be told apart.

    `verdict_by_hash` maps a patch hash to a graded bool. Hash rather than instance, because
    the whole point is that one instance now has several gradable artefacts.
    """
    res = {"rescued": 0, "regressed": 0, "stable_correct": 0, "stable_wrong": 0,
           "ungradable": 0, "instances_considered": 0}
    for inst, rows in snapshots.items():
        hashes = [r.get("patch_sha256_16") for r in rows]
        if len(set(hashes)) < 2:
            continue
        res["instances_considered"] += 1
        verdicts = [verdict_by_hash.get(h) for h in hashes]
        known = [v for v in verdicts if v is not None]
        if len(known) < 2:
            res["ungradable"] += 1
            continue
        first, last = known[0], known[-1]
        if not first and last:
            res["rescued"] += 1
        elif first and not last:
            res["regressed"] += 1
        elif first and last:
            res["stable_correct"] += 1
        else:
            res["stable_wrong"] += 1
    res["reading"] = (
        "rescue without regression is not the measurement. A policy that rescues 30% and "
        "breaks 20% is not a 30% improvement, and the completion floor cannot see the "
        "difference because both attempts report DONE.")
    return res


def summary(directory=SNAP_DIR):
    snaps = load(directory)
    total = sum(len(v) for v in snaps.values())
    return {
        "instances_with_snapshots": len(snaps),
        "snapshots": total,
        "instances_with_differing_attempts": distinct_attempts(snaps),
        "note": ("instances whose attempts produced identical patches cannot contribute to a "
                 "rescue or regression rate; grading them twice answers nothing."),
    }


def _main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=SNAP_DIR)
    a = ap.parse_args()
    print(json.dumps(summary(a.dir), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
