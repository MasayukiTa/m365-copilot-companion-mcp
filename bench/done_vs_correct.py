"""How often "DONE" was right.

THE GAP THIS FILE MEASURES. bench/retry_floor.py carries a warning it has never been able to
lift: "DONE is the worker reporting that it finished; nothing external checked the answer. A
mechanism asked to beat these rates is being asked to beat a claim, not a result. An oracle is
required before any of this is an accuracy floor."

A graded slice IS that oracle. For every instance in it there are two facts that have never
been put beside each other:

    the fleet's own outcome   -- DONE, STUCK, VERIFY_FAILED, ...   (a self-report)
    the grader's verdict      -- resolved / not resolved            (an external check)

Their disagreement is the number. A worker that says DONE and is wrong is the failure mode
every self-reported metric in this repository is blind to, and until a slice was graded there
was nothing to divide by.

WHAT THIS IS NOT. It is not pass@k over graded results: the run captures one patch per
instance, so there is exactly one graded attempt each. Attempt COUNTS are still joined,
because "did retrying buy correctness or only completion" is answerable from them and is a
different question from pass@k.
"""
from __future__ import annotations

import io
import json
import os
from collections import Counter, defaultdict


def load_eval(path):
    """instance_id -> bool. The grader's verdict file."""
    with io.open(path, encoding="utf-8") as fh:
        m = json.load(fh)
    if not isinstance(m, dict):
        raise ValueError("eval results must be an object of instance_id -> verdict")
    return {k: bool(v) for k, v in m.items()}


def load_ledger(path):
    try:
        with io.open(path, encoding="utf-8") as fh:
            rows = json.load(fh)
    except (OSError, ValueError):
        return []
    return rows if isinstance(rows, list) else []


def _instance_of(goal, instance_ids):
    """Which instance a ledger row belongs to, or None.

    Joined on the instance id appearing in the goal text. A goal that matches two instances
    joins to NEITHER -- an ambiguous join is worse than a missing one, because it silently
    attributes one worker's attempts to another instance.
    """
    if not goal:
        return None
    hits = [i for i in instance_ids if i in goal]
    return hits[0] if len(hits) == 1 else None


def attempts_by_instance(rows, instance_ids):
    out = defaultdict(list)
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        inst = _instance_of(r.get("goal") or "", instance_ids)
        if inst:
            out[inst].append(r)
    return out


def report(eval_path, ledger_path):
    verdicts = load_eval(eval_path)
    ids = list(verdicts)
    rows = load_ledger(ledger_path)
    att = attempts_by_instance(rows, ids)

    resolved = sum(1 for v in verdicts.values() if v)
    n = len(verdicts)

    # THE CROSS-TABULATION. Every instance falls in exactly one cell, and the cell that
    # matters is (said DONE, was wrong).
    cells = Counter()
    per_attempt_bucket = defaultdict(lambda: [0, 0])   # attempts -> [resolved, total]
    said_done_wrong = []
    for inst, ok in verdicts.items():
        a = att.get(inst) or []
        outcomes = [x.get("outcome") for x in a]
        claimed = "DONE" in outcomes
        cells[(claimed, ok)] += 1
        if claimed and not ok:
            said_done_wrong.append(inst)
        k = len(a)
        if k:
            per_attempt_bucket[k][1] += 1
            per_attempt_bucket[k][0] += int(ok)

    claimed_done = cells[(True, True)] + cells[(True, False)]
    return {
        "measures": "external correctness from a grader, joined to the fleet's self-report",
        "instances_graded": n,
        "resolved": resolved,
        "resolved_rate": (resolved / n) if n else None,
        "instances_with_ledger_attempts": len(att),
        # The four cells, spelled out rather than abbreviated, because a reader who has to
        # decode (True, False) will decode it wrong at least once.
        "said_done_and_correct": cells[(True, True)],
        "said_done_and_wrong": cells[(True, False)],
        "never_said_done_but_correct": cells[(False, True)],
        "never_said_done_and_wrong": cells[(False, False)],
        # THE HEADLINE. Of the times a worker claimed to have finished, how often was it right.
        "precision_of_done": (cells[(True, True)] / claimed_done) if claimed_done else None,
        "by_attempt_count": {str(k): {"resolved": v[0], "total": v[1],
                                      "rate": (v[0] / v[1]) if v[1] else None}
                             for k, v in sorted(per_attempt_bucket.items())},
        "said_done_and_wrong_ids": sorted(said_done_wrong)[:20],
        # Kept beside the numbers so it is not dropped in the retelling.
        "not_pass_at_k": (
            "one patch was captured per instance, so there is exactly one graded attempt "
            "each. Attempt counts are joined to ask whether retrying bought correctness, "
            "which is a different question from pass@k and cannot substitute for it."),
    }


def _main():
    import argparse
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--eval", required=True, help="eval_results.json from the grader")
    ap.add_argument("--ledger", default=os.path.join(here, ".fleet", "history.json"))
    a = ap.parse_args()
    out = report(a.eval, a.ledger)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
