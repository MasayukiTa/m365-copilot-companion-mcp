# -*- coding: utf-8 -*-
"""Proposer / solver / judge role separation check (slice G3, report only).

WHAT DECISION THIS SERVES. Whether a candidate was judged by someone other than the party that
proposed it: a proposer must never hold the final evaluation authority over its own candidate. It
reads candidate records (id, proposer, solver, judge, verdict, who recorded the verdict) and an
optional list of delegation edges, finds every breach of that rule by deterministic checks, and
returns a report with counts, the violation list with a reason each, and a pass/fail. It returns a
report and nothing else: it changes no record, writes no file (not under .fleet either), deletes
nothing, calls no network, no fleet and no model, runs no process and has no clock.

Default off: no runtime module imports it, only scripts/org_role_separation.py, run by hand.

THE RECORD (a dict; every field optional on input, a missing one is reported, never guessed).
  candidate_id  string, names the candidate in the report.
  proposer_id   who produced the candidate.
  solver_id     who worked the task the candidate answers (may equal the proposer).
  judge_id      who is assigned the final evaluation.
  verdict       the recorded verdict; None or an empty string means no verdict yet.
  verdict_by    who recorded the verdict.
THE EDGES. A delegation edge says `from` handed work to `to`: {"from": a, "to": b} or [a, b].
Edges are directed and may form cycles; reachability is followed with a visited set.

THE CHECKS (CODES, in report order). A candidate can raise several.
  missing_judge              judge_id is absent or empty. No other judge check can run, and a
                             verdict on such a candidate is not checked further for the same reason.
  judge_is_proposer          judge_id equals proposer_id.
  judge_is_solver            judge_id equals solver_id of the same candidate.
  judge_descends_from_proposer  the judge is reachable from the proposer along delegation edges
                             (the proposer's own delegate judges its work: self-evaluation through
                             delegation).
  judge_ancestor_of_proposer the proposer is reachable from the judge along delegation edges (the
                             proposer works under the judge, which makes the judge's verdict
                             not independent in the other direction).
  verdict_not_by_judge       a verdict is recorded and verdict_by names someone other than judge_id.
  verdict_unattributed       a verdict is recorded and verdict_by is absent: who held the final
                             authority cannot be shown, so it is not accepted as the judge's.
A record that is not a dict is `malformed`. Ids compare after stripping surrounding whitespace, case
sensitive. PASS means no violation and no malformed record; an empty input passes with zero checked.
"""
from __future__ import annotations

import json

VERSION = 1
MISSING_JUDGE, JUDGE_IS_PROPOSER, JUDGE_IS_SOLVER = (
    "missing_judge", "judge_is_proposer", "judge_is_solver")
JUDGE_DESCENDS, JUDGE_ANCESTOR = "judge_descends_from_proposer", "judge_ancestor_of_proposer"
VERDICT_NOT_BY_JUDGE, VERDICT_UNATTRIBUTED = "verdict_not_by_judge", "verdict_unattributed"
CODES = (MISSING_JUDGE, JUDGE_IS_PROPOSER, JUDGE_IS_SOLVER, JUDGE_DESCENDS, JUDGE_ANCESTOR,
         VERDICT_NOT_BY_JUDGE, VERDICT_UNATTRIBUTED)


def _ident(value):
    """A non-empty stripped string, or None."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _has_verdict(value):
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def build_graph(edges):
    """Return (children, parents) adjacency maps from the edge list; bad edges are skipped."""
    children, parents = {}, {}
    for edge in edges if isinstance(edges, list) else []:
        if isinstance(edge, dict):
            a, b = _ident(edge.get("from")), _ident(edge.get("to"))
        elif isinstance(edge, (list, tuple)) and len(edge) == 2:
            a, b = _ident(edge[0]), _ident(edge[1])
        else:
            continue
        if a is None or b is None:
            continue
        children.setdefault(a, set()).add(b)
        parents.setdefault(b, set()).add(a)
    return children, parents


def reachable(graph, start):
    """Every node reachable from start in one or more steps. Cycle safe; start is included only
    when a cycle leads back to it."""
    seen, stack = set(), list(graph.get(start, ()))
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.get(node, ()))
    return seen


def check_record(record, children, parents):
    """Return the list of (code, reason) for one candidate record. Pure."""
    found = []
    proposer, solver = _ident(record.get("proposer_id")), _ident(record.get("solver_id"))
    judge, by = _ident(record.get("judge_id")), _ident(record.get("verdict_by"))
    if judge is None:
        return [(MISSING_JUDGE, "judge_id is absent or empty")]
    if proposer is not None and judge == proposer:
        found.append((JUDGE_IS_PROPOSER, "judge %s is the proposer" % judge))
    if solver is not None and judge == solver:
        found.append((JUDGE_IS_SOLVER, "judge %s is the solver" % judge))
    if proposer is not None and judge != proposer:
        if judge in reachable(children, proposer):
            found.append((JUDGE_DESCENDS, "judge %s is a delegate of proposer %s" % (judge, proposer)))
        if judge in reachable(parents, proposer):
            found.append((JUDGE_ANCESTOR, "proposer %s is a delegate of judge %s" % (proposer, judge)))
    if _has_verdict(record.get("verdict")):
        if by is None:
            found.append((VERDICT_UNATTRIBUTED, "a verdict is recorded without verdict_by"))
        elif by != judge:
            found.append((VERDICT_NOT_BY_JUDGE, "verdict recorded by %s, assigned judge is %s" % (by, judge)))
    return found


def build_report(records, edges=None):
    """Check every candidate record against the role separation rule. Pure."""
    rows = records if isinstance(records, list) else []
    children, parents = build_graph(edges)
    counts = {c: 0 for c in CODES}
    violations, skipped = [], []
    malformed = 0
    for index, rec in enumerate(rows):
        if not isinstance(rec, dict):
            malformed += 1
            skipped.append({"index": index, "code": "malformed"})
            continue
        cid = rec.get("candidate_id")
        cid = cid if isinstance(cid, str) else ""
        for code, reason in check_record(rec, children, parents):
            counts[code] += 1
            violations.append({"index": index, "candidate_id": cid, "code": code, "reason": reason})
    violations.sort(key=lambda v: (v["candidate_id"], v["index"], CODES.index(v["code"])))
    flagged = len({v["index"] for v in violations})
    checked = len(rows) - malformed
    return {"version": VERSION, "total": len(rows), "checked": checked, "malformed": malformed,
            "flagged_candidates": flagged, "clean_candidates": checked - flagged,
            "violation_counts": counts, "violations_total": len(violations),
            "violations": violations, "skipped": skipped,
            "passed": not violations and not malformed}


def to_json(report):
    """Stable text form of a report."""
    return json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
