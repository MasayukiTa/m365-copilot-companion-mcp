# -*- coding: utf-8 -*-
"""Per-agent reputation report with sparse-data, gaming and collusion resistance (slice G5, report only).

WHAT DECISION THIS SERVES. How far a person reading the report may trust an agent's evaluation
history: whether an agent's reputation rests on enough independent evaluators to mean anything, or
on a few friendly ones. It reads evaluation records and returns, per agent, a shrunk reputation, a
confidence label and the number of evaluators that really contributed, plus the list of flagged
evaluations with a reason each and a pass/fail. It returns a report and nothing else: it changes no
record, writes no file (not under .fleet either), deletes nothing, calls no network, no fleet and no
model, runs no process and has no clock (a timestamp is carried as given and never read).

Default off: no runtime module imports it, only scripts/org_reputation.py, run by hand.

THE RECORD (a dict). evaluator_id and subject_id are non-empty strings, score is a number in [0, 1]
(not a bool, not NaN or infinite), task_id is an optional non-empty string, timestamp is an optional
int (not a bool). Anything else is `malformed`: skipped and counted, never repaired or guessed.
Ids compare after stripping surrounding whitespace, case sensitive.

THE RESISTANCES (CODES, in report order).
  self_evaluation        evaluator_id equals subject_id. Ignored (weight 0) and listed.
  duplicate_evaluation   the same (evaluator, subject, task_id) already appeared earlier in the
                         input; only the first counts. Only records that carry a task_id can repeat.
  reciprocal_high_pair   A rates B high and B rates A high (each side's mean score on the other is at
                         least HIGH_SCORE). Every evaluation in both directions is down-weighted.
  tight_cluster          three or more agents that all reach each other through high ratings and
                         whose evaluations given mostly (at least CLUSTER_INSIDE_FRACTION) go to
                         each other. Evaluations inside the cluster are down-weighted.
  evaluator_capped       informational: one evaluator's total weight on one subject exceeded
                         EVALUATOR_CAP and was cut to it, so a flood of evaluations from one
                         evaluator counts as at most EVALUATOR_CAP of one vote. Does not fail.

THE ARITHMETIC. Each counted evaluation has weight 1, or COLLUSION_WEIGHT if flagged. For a subject,
an evaluator's evaluations are averaged by weight, and that evaluator's weight is the sum, capped at
EVALUATOR_CAP. Reputation = (PRIOR_STRENGTH * PRIOR_MEAN + sum(weight * mean)) / (PRIOR_STRENGTH +
sum(weight)): with little data it stays near PRIOR_MEAN. Confidence is `unknown` when fewer than
MIN_EVALUATORS distinct evaluators contributed (reputation is still printed, shrunk, but is not to be
read as a measurement), `low` below LOW_EVALUATORS, else `ok`.

PASS means no malformed record and no self_evaluation, duplicate_evaluation, reciprocal_high_pair or
tight_cluster flag. evaluator_capped and an `unknown` confidence do not fail. An empty input passes
with zero checked.
"""
from __future__ import annotations

import json
import math

VERSION = 1
PRIOR_MEAN = 0.5
PRIOR_STRENGTH = 4.0
MIN_EVALUATORS = 3
LOW_EVALUATORS = 6
EVALUATOR_CAP = 1.0
HIGH_SCORE = 0.8
COLLUSION_WEIGHT = 0.25
CLUSTER_MIN_SIZE = 3
CLUSTER_INSIDE_FRACTION = 0.8

SELF_EVAL, DUPLICATE = "self_evaluation", "duplicate_evaluation"
RECIPROCAL, CLUSTER, CAPPED = "reciprocal_high_pair", "tight_cluster", "evaluator_capped"
CODES = (SELF_EVAL, DUPLICATE, RECIPROCAL, CLUSTER, CAPPED)
FAILING_CODES = (SELF_EVAL, DUPLICATE, RECIPROCAL, CLUSTER)
UNKNOWN, LOW, OK = "unknown", "low", "ok"


def _ident(value):
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def parse_record(record):
    """Return (clean dict, None) or (None, reason). Pure."""
    if not isinstance(record, dict):
        return None, "record is not an object"
    ev, sub = _ident(record.get("evaluator_id")), _ident(record.get("subject_id"))
    if ev is None:
        return None, "evaluator_id is absent or empty"
    if sub is None:
        return None, "subject_id is absent or empty"
    score = record.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None, "score is not a number"
    if not math.isfinite(score) or score < 0 or score > 1:
        return None, "score is outside [0, 1]"
    task = record.get("task_id")
    if task is not None:
        task = _ident(task)
        if task is None:
            return None, "task_id is present but not a non-empty string"
    ts = record.get("timestamp")
    if ts is not None and (isinstance(ts, bool) or not isinstance(ts, int)):
        return None, "timestamp is present but not an int"
    return {"evaluator": ev, "subject": sub, "score": float(score), "task": task,
            "timestamp": ts}, None


def _pair_means(counted):
    """Mean score per directed (evaluator, subject) pair over counted evaluations."""
    sums = {}
    for e in counted:
        key = (e["evaluator"], e["subject"])
        s, n = sums.get(key, (0.0, 0))
        sums[key] = (s + e["score"], n + 1)
    return {k: s / n for k, (s, n) in sums.items()}


def _mutual_groups(high_edges):
    """Groups of agents that all reach each other along high edges (strongly connected sets of
    size two or more). Deterministic; cycle safe."""
    adj = {}
    for a, b in high_edges:
        adj.setdefault(a, set()).add(b)
    reach = {}
    for start in adj:
        seen, stack = set(), list(adj[start])
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(adj.get(node, ()))
        reach[start] = seen
    groups, done = [], set()
    for node in sorted(reach):
        if node in done:
            continue
        group = {node} | {m for m in reach[node] if m in reach and node in reach[m]}
        done |= group
        if len(group) >= 2:
            groups.append(sorted(group))
    return groups


def _confidence(n_evaluators):
    if n_evaluators < MIN_EVALUATORS:
        return UNKNOWN
    if n_evaluators < LOW_EVALUATORS:
        return LOW
    return OK


def build_report(records):
    """Compute the reputation report from evaluation records. Pure."""
    rows = records if isinstance(records, list) else []
    flags, skipped = [], []
    parsed = []
    for index, rec in enumerate(rows):
        clean, why = parse_record(rec)
        if clean is None:
            skipped.append({"index": index, "code": "malformed", "reason": why})
            continue
        clean["index"] = index
        parsed.append(clean)

    counted, seen_keys = [], set()
    for e in parsed:
        if e["evaluator"] == e["subject"]:
            flags.append({"code": SELF_EVAL, "index": e["index"], "agents": [e["evaluator"]],
                          "reason": "%s evaluated itself; ignored" % e["evaluator"]})
            continue
        if e["task"] is not None:
            key = (e["evaluator"], e["subject"], e["task"])
            if key in seen_keys:
                flags.append({"code": DUPLICATE, "index": e["index"],
                              "agents": [e["evaluator"], e["subject"]],
                              "reason": "%s already evaluated %s for task %s; only the first counts"
                                        % (e["evaluator"], e["subject"], e["task"])})
                continue
            seen_keys.add(key)
        counted.append(e)

    means = _pair_means(counted)
    high_edges = sorted(k for k, m in means.items() if m >= HIGH_SCORE)
    downweighted = set()
    for a, b in high_edges:
        if a < b and (b, a) in means and means[(b, a)] >= HIGH_SCORE:
            flags.append({"code": RECIPROCAL, "index": None, "agents": [a, b],
                          "reason": "%s and %s rate each other high (%.3f and %.3f)"
                                    % (a, b, means[(a, b)], means[(b, a)])})
            for e in counted:
                if (e["evaluator"], e["subject"]) in ((a, b), (b, a)):
                    downweighted.add(e["index"])
    for group in _mutual_groups(high_edges):
        if len(group) < CLUSTER_MIN_SIZE:
            continue
        members = set(group)
        out_total = sum(1 for e in counted if e["evaluator"] in members)
        out_inside = sum(1 for e in counted if e["evaluator"] in members and e["subject"] in members)
        if out_total and out_inside / out_total >= CLUSTER_INSIDE_FRACTION:
            flags.append({"code": CLUSTER, "index": None, "agents": group,
                          "reason": "%d agents rate each other high and give %d of %d evaluations "
                                    "inside the group" % (len(group), out_inside, out_total)})
            for e in counted:
                if e["evaluator"] in members and e["subject"] in members:
                    downweighted.add(e["index"])

    by_subject = {}
    for e in counted:
        by_subject.setdefault(e["subject"], {}).setdefault(e["evaluator"], []).append(e)
    agents = []
    for subject in sorted(by_subject):
        total_w = total_ws = 0.0
        contributing = 0
        for evaluator in sorted(by_subject[subject]):
            evs = by_subject[subject][evaluator]
            ws = [COLLUSION_WEIGHT if e["index"] in downweighted else 1.0 for e in evs]
            raw_w = sum(ws)
            mean = sum(w * e["score"] for w, e in zip(ws, evs)) / raw_w
            w = min(raw_w, EVALUATOR_CAP)
            if raw_w > EVALUATOR_CAP:
                flags.append({"code": CAPPED, "index": None, "agents": [evaluator, subject],
                              "reason": "%s weight on %s %.3f cut to %.3f (%d evaluations)"
                                        % (evaluator, subject, raw_w, EVALUATOR_CAP, len(evs))})
            total_w += w
            total_ws += w * mean
            contributing += 1
        rep = (PRIOR_STRENGTH * PRIOR_MEAN + total_ws) / (PRIOR_STRENGTH + total_w)
        agents.append({"agent_id": subject, "reputation": round(rep, 6),
                       "confidence": _confidence(contributing),
                       "contributing_evaluators": contributing,
                       "evaluations_counted": sum(len(v) for v in by_subject[subject].values()),
                       "effective_weight": round(total_w, 6)})

    order = {c: i for i, c in enumerate(CODES)}
    flags.sort(key=lambda f: (order[f["code"]], f["index"] if f["index"] is not None else -1,
                              f["agents"]))
    counts = {c: 0 for c in CODES}
    for f in flags:
        counts[f["code"]] += 1
    failing = sum(counts[c] for c in FAILING_CODES)
    return {"version": VERSION, "total": len(rows), "checked": len(parsed),
            "malformed": len(skipped), "counted": len(counted),
            "downweighted_evaluations": len(downweighted),
            "flag_counts": counts, "flags_total": len(flags), "flags": flags,
            "agents": agents, "skipped": skipped,
            "passed": failing == 0 and not skipped}


def to_json(report):
    return json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
