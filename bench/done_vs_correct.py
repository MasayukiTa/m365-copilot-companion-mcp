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


def worktree_map_for(slice_path):
    """instance_id -> worktree path, reconstructed from the slice.

    THE GOALS DO NOT NAME THE INSTANCE. They name the checkout:

        The repository is checked out locally at:
          ...\.fleet\swe\work\p03

    The first version of this file joined on the instance id appearing in the goal text and
    matched nothing -- 40 instances, 0 attempts found, every one filed as "never said DONE",
    which reads exactly like a fleet that never claimed anything. The id is not in the text.

    pro_stage_goals assigns pNN by position in the SORTED slice, so the mapping is
    reproducible from the slice file alone. That matters because pro_wt_map.json is rewritten
    by every batch and no longer describes the run being graded.
    """
    import os
    rows = json.load(io.open(slice_path, encoding="utf-8-sig"))
    ids = sorted(r["instance_id"] for r in rows)
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ".fleet", "swe", "work")
    return {inst: os.path.join(root, "p%02d" % i) for i, inst in enumerate(ids)}


def attempts_by_instance(rows, wt_map):
    """instance_id -> its ledger rows, joined on the worktree path the goal names.

    Reuses swe_run_facts._mentions_path rather than restating it: `.../p1` must not match a
    goal about `.../p10`, and the same predicate written twice is the shape that drifts.
    """
    import os as _os
    import sys as _sys
    _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    from bench.swe_run_facts import _mentions_path, _norm
    out = defaultdict(list)
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        goal = _norm(r.get("goal") or "")
        hits = [inst for inst, path in wt_map.items() if _mentions_path(goal, _norm(path))]
        # An ambiguous goal joins to NEITHER: silently lending one worker's attempts to
        # another instance produces a number that looks complete and is wrong.
        if len(hits) == 1:
            out[hits[0]].append(r)
    return out


def report(eval_path, ledger_path, slice_path):
    verdicts = load_eval(eval_path)
    rows = load_ledger(ledger_path)
    att = attempts_by_instance(rows, worktree_map_for(slice_path))

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
    ap.add_argument("--slice", default=os.path.join(here, ".fleet", "swe", "pro_slice40_fresh.json"),
                    help="the slice the run used; the pNN mapping is derived from it")
    a = ap.parse_args()
    out = report(a.eval, a.ledger, a.slice)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
