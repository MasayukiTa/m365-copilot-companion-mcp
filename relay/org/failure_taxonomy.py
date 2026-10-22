# -*- coding: utf-8 -*-
"""Failure taxonomy and replay curriculum order (slice G1, report only).

WHAT DECISION THIS SERVES. Which kinds of failure a person should replay first when reviewing past
episodes. It reads episode records in the shape relay/selfimprove/episode_record.py produces
(`build` or `compact`), sorts every failed episode into one of a small fixed set of classes by
deterministic rules, and orders the classes into a replay curriculum by how often each occurred
and how recently. It returns a report and nothing else: it replays nothing, writes no file
(not under .fleet either), deletes nothing, calls no network, no fleet and no model, runs no
process and has no clock. The recency comes from the `ts` the records carry.

Default off: no runtime module imports it, only scripts/org_failure_taxonomy.py, run by hand.

THE CLASSES (a fixed tuple, CLASSES; first rule that matches wins, so the order is the priority).
  security_failure      outcome.security_success is False, or the record lists security events.
                        Reason it comes first: a run that breaks a guard is the failure to look at
                        even when it also failed functionally.
  timeout_or_budget     outcome.timed_out is True (the run was cut off, so any functional result is
                        not a measurement of the harness).
  verification_failure  verification.checks_failed is a positive number, or outcome.verification_success
                        is False (the run's own proof did not hold).
  functional_failure    outcome.functional_success is False.
  unexplained_failure   the record carries a non-empty failure_class label but none of the signals
                        above. Kept apart on purpose: a label with no supporting outcome cannot be
                        placed in a class, and folding it into one would invent evidence.
A record that is none of these is NOT a failure and is counted as a success when its outcome says
so (functional_success True) and as `no_outcome` otherwise. Absent stays absent: a record with no
outcome and no label is never counted as a pass or a failure. A record that is not a dict is
`malformed`.

THE CURRICULUM. Classes with at least one episode, sorted by (count descending, latest `ts`
descending, class name). A record with no numeric `ts` has an unknown time: it counts toward the
frequency and never toward the recency, and a class whose episodes all lack a `ts` sorts after the
classes whose recency is known at the same count. Inside a class the episodes are listed newest
first, then by episode id. No cap on the number of classes (there are five) or of listed episodes:
nothing measured gives a basis for a number.
"""
from __future__ import annotations

import json

VERSION = 1
SECURITY, TIMEOUT, VERIFICATION, FUNCTIONAL, UNEXPLAINED = (
    "security_failure", "timeout_or_budget", "verification_failure", "functional_failure",
    "unexplained_failure")
CLASSES = (SECURITY, TIMEOUT, VERIFICATION, FUNCTIONAL, UNEXPLAINED)
SUCCESS, NO_OUTCOME, MALFORMED = "success", "no_outcome", "malformed"


def _num(value):
    """A real number, or None. bool is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _nonempty_list(value):
    return isinstance(value, list) and len(value) > 0


def classify_record(record):
    """Return (class, reason). class is one of CLASSES, SUCCESS, NO_OUTCOME or MALFORMED."""
    if not isinstance(record, dict):
        return MALFORMED, "record is not an object"
    outcome = record.get("outcome")
    outcome = outcome if isinstance(outcome, dict) else {}
    verification = record.get("verification")
    verification = verification if isinstance(verification, dict) else {}
    if outcome.get("security_success") is False:
        return SECURITY, "outcome.security_success is false"
    if _nonempty_list(record.get("security_events")) or (_num(record.get("security_events_count")) or 0) > 0:
        return SECURITY, "the record lists security events"
    if outcome.get("timed_out") is True:
        return TIMEOUT, "outcome.timed_out is true"
    if (_num(verification.get("checks_failed")) or 0) > 0:
        return VERIFICATION, "verification.checks_failed is positive"
    if outcome.get("verification_success") is False:
        return VERIFICATION, "outcome.verification_success is false"
    if outcome.get("functional_success") is False:
        return FUNCTIONAL, "outcome.functional_success is false"
    label = record.get("failure_class")
    if isinstance(label, str) and label.strip():
        return UNEXPLAINED, "failure_class is labelled but no outcome signal supports a class"
    if outcome.get("functional_success") is True:
        return SUCCESS, "outcome.functional_success is true"
    return NO_OUTCOME, "no outcome and no failure label"


def _bump(table, key):
    key = key if isinstance(key, str) and key.strip() else "(none)"
    table[key] = table.get(key, 0) + 1


def build_report(records):
    """Classify the records and order the classes into a replay curriculum. Pure."""
    rows = records if isinstance(records, list) else []
    counts = {SUCCESS: 0, NO_OUTCOME: 0, MALFORMED: 0}
    classes = {c: [] for c in CLASSES}
    skipped = []
    for index, rec in enumerate(rows):
        cls, reason = classify_record(rec)
        if cls in classes:
            ts = _num(rec.get("ts"))
            eid = rec.get("episode_id")
            classes[cls].append({"index": index,
                                 "episode_id": eid if isinstance(eid, str) else "",
                                 "ts": ts, "reason": reason, "rec": rec})
        else:
            counts[cls] += 1
            if cls == MALFORMED:
                skipped.append({"index": index, "code": MALFORMED})
    out_classes = {}
    for cls in CLASSES:
        eps = sorted(classes[cls], key=lambda e: (e["ts"] is None, -(e["ts"] or 0), e["episode_id"], e["index"]))
        labels, tasks = {}, {}
        for e in eps:
            _bump(labels, e["rec"].get("failure_class"))
            _bump(tasks, e["rec"].get("task_class"))
        known = [e["ts"] for e in eps if e["ts"] is not None]
        out_classes[cls] = {
            "count": len(eps),
            "latest_ts": max(known) if known else None,
            "without_ts": len(eps) - len(known),
            "labels": dict(sorted(labels.items())),
            "task_classes": dict(sorted(tasks.items())),
            "episodes": [{"index": e["index"], "episode_id": e["episode_id"], "ts": e["ts"],
                          "reason": e["reason"]} for e in eps],
        }
    order = sorted((c for c in CLASSES if out_classes[c]["count"]),
                   key=lambda c: (-out_classes[c]["count"], out_classes[c]["latest_ts"] is None,
                                  -(out_classes[c]["latest_ts"] or 0), c))
    curriculum = [{"rank": i + 1, "class": c, "count": out_classes[c]["count"],
                   "latest_ts": out_classes[c]["latest_ts"],
                   "episode_ids": [e["episode_id"] for e in out_classes[c]["episodes"]]}
                  for i, c in enumerate(order)]
    failures = sum(out_classes[c]["count"] for c in CLASSES)
    return {"version": VERSION, "total": len(rows), "failures": failures,
            "successes": counts[SUCCESS], "no_outcome": counts[NO_OUTCOME],
            "malformed": counts[MALFORMED], "classes": out_classes,
            "curriculum": curriculum, "skipped": skipped}


def to_json(report):
    """Stable text form of a report."""
    return json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
