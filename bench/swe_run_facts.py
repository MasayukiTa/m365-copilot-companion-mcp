"""Join what the fleet RECORDED about a run onto the instances a scorecard grades.

WHY THIS EXISTS. Two facts were being measured and thrown away at the same seam.

  * `pro_capture.py` writes a prediction row for every worktree that still exists, whatever
    the worker's outcome was, including an empty diff. A goal a human STOPPED, and a goal
    whose connection never established, both arrive at the grader as `patch=""`, grade as
    unresolved, and enter the pass@1 denominator as failures. Neither is a failure of the
    system under test: one measures the operator, the other measures the environment.
  * `pro_record_result.py` built its behaviour descriptors with `"turns": 0` -- a hardcoded
    constant. That is worse than a missing field. A missing field is visibly missing; a field
    that is always zero silently makes every turn-based descriptor a statement about nothing.

Both were already recorded. `.fleet/history.json` carries `outcome` AND `turn` for every
worker. The data never crossed from the fleet's ledger into the benchmark's, because the
capture step reads git worktrees and the record step reads predictions, and no step read the
ledger. This module is that missing step, and nothing more: it JOINS, it does not judge.
The scoring side lives in relay/outcomes.py, where the outcome vocabulary is already closed.

THE JOIN KEY. Two records exist and they are not the same shape. The fleet's FINAL snapshot
carries `cwd` -- the worker's working directory, written there so an orchestrator can map
workers back to instances -- and that is an exact key. The rows in `.fleet/history.json` are
thinner and have no cwd, so a second key is needed: a staged SWE goal names the worktree it is
to be solved in, and
`pro_wt_map.json` maps instance_id -> that path. So a history row belongs to an instance when
the instance's worktree path appears in the row's goal text. This is a substring test on a
path, which is why `_norm` exists: the map and the goal text are both written on Windows and
disagree about separators and case more often than they agree.

COVERAGE IS RETURNED, NOT ASSUMED. A join that matches nothing must not read as "no
exclusions" -- that is exactly today's behaviour wearing a new name and claiming to be an
improvement. `join_report` returns how many of the graded ids were actually found, so a
caller can refuse to score a run whose ledger it could not read.
"""
from __future__ import annotations

import json
import os


def _norm(p: str) -> str:
    """A path as it compares, not as it was written. Windows writes the same worktree three
    ways across a map file, a goal string and a log line."""
    return os.path.normcase((p or "").replace("\\", "/").rstrip("/"))


#: What may follow a path for it to count as named rather than as a prefix of a
#: longer one. Built from code points: the set contains both quote characters and a
#: backslash, and spelling it as a literal is how the previous attempt broke.
_BOUNDARY = frozenset(chr(c) for c in (47, 92, 32, 9, 13, 10, 34, 39, 44, 59, 58,
                                       41, 93, 125))


def _mentions_path(text: str, path: str) -> bool:
    """True when `text` names `path` as a path, not merely as a prefix of a longer one.

    `.../p1` must not match text about `.../p10`. The character after the match has to be a
    separator or the end of the string.
    """
    if not path:
        return False
    start = 0
    while True:
        i = text.find(path, start)
        if i < 0:
            return False
        end = i + len(path)
        if end >= len(text) or text[end] in _BOUNDARY:
            return True
        start = i + 1


def facts_from_history(wt_map, history_rows):
    """instance_id -> {outcome, turns, attempts} for every instance the ledger knows about.

    `outcome` is the BEST outcome across attempts, under the same rule the fleet already uses
    to collapse retries: a DONE anywhere in the family is the answer, and a later failure does
    not retract an earlier success.

    `turns` is the SUM across attempts, and the asymmetry with `outcome` is deliberate. The
    two fields answer different questions: "did this instance get solved" is about the best
    attempt, "what did this instance cost" is about all of them. Reporting the last attempt's
    turn count would hide the price of every retry that preceded it -- the precise error
    `AI Agents That Matter` names, where an expensive scaffold outscores a cheap one because
    only the accuracy was carried forward.
    """
    by_path = {}
    collided = set()
    for inst, path in (wt_map or {}).items():
        n = _norm(path)
        if not n:
            continue
        if n in by_path and by_path[n] != inst:
            # TWO INSTANCES ON ONE PATH. Silently letting the later entry own every worker
            # that ran there would attribute one instance's outcome to another. Neither can
            # be trusted, so neither is joined.
            collided.add(n)
        by_path[n] = inst
    for n in collided:
        by_path.pop(n, None)

    facts = {}
    for row in history_rows or []:
        # `cwd` FIRST when the record carries it. The fleet's final snapshot writes the
        # worker's working directory precisely so an orchestrator can map workers back to
        # instances, which makes it an exact key rather than a substring guess. The thinner
        # rows in history.json have no cwd, so the goal-text match remains the fallback --
        # both are tried, because the two records are not the same shape.
        cwd = _norm(row.get("cwd") or "")
        inst = by_path.get(cwd) if cwd else None
        if inst is None:
            goal = _norm(row.get("goal") or "")
            if not goal:
                continue
            # PATH BOUNDARIES, AND NO GUESSING BETWEEN CANDIDATES.
            #
            # A bare substring test matched `.../p1` inside text naming `.../p10`, and took
            # the first entry that hit -- so a prefix collision picked an instance by
            # dictionary order. It also matched a path merely MENTIONED in the goal, and a
            # staged goal quotes an issue body that can name anything.
            #
            # A path only matches when what follows it in the text is a separator or nothing,
            # and an ambiguous goal (two different instances' paths present) joins to NEITHER.
            # The cost of refusing is one unjoined row, which stays in the denominator; the
            # cost of guessing is an outcome attributed to the wrong instance.
            hits = {i for p, i in by_path.items() if _mentions_path(goal, p)}
            if len(hits) != 1:
                continue
            inst = hits.pop()
        if inst is None:
            continue
        f = facts.setdefault(inst, {"outcome": None, "turns": 0, "attempts": 0})
        f["attempts"] += 1
        try:
            f["turns"] += int(row.get("turn") or 0)
        except (TypeError, ValueError):
            pass
        outcome = row.get("outcome")
        # DONE wins and is never retracted; otherwise the most recent word stands.
        if f["outcome"] != "DONE" and outcome:
            f["outcome"] = outcome
    return facts


def join_report(graded_ids, facts):
    """How much of the graded slice the ledger actually covered.

    A caller that ignores this can report a clean run on a ledger it failed to read.
    """
    ids = list(graded_ids or [])
    found = [i for i in ids if i in (facts or {})]
    missing = [i for i in ids if i not in (facts or {})]
    return {
        "graded": len(ids),
        "joined": len(found),
        "missing": missing,
        "coverage": (len(found) / len(ids)) if ids else None,
    }


def load(wtmap_path, history_path):
    """Read both ledgers from disk. Missing files yield empty facts, never an exception --
    an old run has no ledger to read, and that must degrade to 'no facts', which `join_report`
    then reports as zero coverage rather than as a healthy run."""
    def _read(path, default):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    wt = _read(wtmap_path, {})
    hist = _read(history_path, [])
    if not isinstance(wt, dict):
        wt = {}
    if not isinstance(hist, list):
        hist = hist.get("runs") if isinstance(hist, dict) else []
        hist = hist or []
    return facts_from_history(wt, hist)
