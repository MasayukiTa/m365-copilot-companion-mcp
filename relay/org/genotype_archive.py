# -*- coding: utf-8 -*-
"""Multi-objective archive for genotype results (slice E4): keep only the Pareto-front entries.

WHAT DECISION THIS SERVES. When several genotypes have been measured (relay/org/genotype_bench),
which of them is not beaten on every axis by another one. A single score hides trade-offs (a
genotype that wins on success but doubles wall time is not "better" than its parent); the front
keeps every genotype that is best at something, so a later step can choose among them. This module
only compares numbers it is handed. It starts no job, submits nothing to the fleet, opens no
network connection and writes no file (nothing under .fleet). Nothing in production calls it
(default off): only scripts/org_genotype_archive.py, run by hand, and only to print a front.

AXES. success is maximised; wall_time, turns, tool_calls, retries, infra_failures, regressions and
human_interventions are minimised (AXES below). An entry is {"id": str, "axes": {axis: number}}.

MISSING AXES. An axis an entry does not carry (absent, non-numeric, bool or NaN) counts as the
worst possible value on that axis. An entry cannot win on an axis it was not measured on, and an
unmeasured genotype cannot hide a measured one. Axis names outside AXES are dropped.

PURE AND ORDER-INDEPENDENT. dominates / pareto_front / insert never mutate their arguments and
insert returns a new archive. The front is the same set whatever order entries arrive in, and the
archive lists it in one canonical order. Entries with identical axes and different ids tie: neither
dominates, so both stay. The same id with identical axes is kept once.
"""
from __future__ import annotations

import json
import math
import numbers

VERSION = 1

# (name, direction) in a fixed order; "max" means higher is better.
AXES = (
    ("success", "max"),
    ("wall_time", "min"),
    ("turns", "min"),
    ("tool_calls", "min"),
    ("retries", "min"),
    ("infra_failures", "min"),
    ("regressions", "min"),
    ("human_interventions", "min"),
)
AXIS_NAMES = tuple(n for n, _ in AXES)


class ArchiveError(ValueError):
    """The input is not an archive or an entry."""


def _number(v):
    if isinstance(v, bool) or not isinstance(v, numbers.Real):
        return None
    v = float(v)
    return None if math.isnan(v) or math.isinf(v) else v


def make_entry(entry_id, axes):
    """A normalised entry: only known axes with finite numeric values are kept."""
    if not isinstance(entry_id, str) or not entry_id:
        raise ArchiveError("an entry needs a non-empty string id")
    axes = axes if isinstance(axes, dict) else {}
    kept = {}
    for name in AXIS_NAMES:
        v = _number(axes.get(name))
        if v is not None:
            kept[name] = v
    return {"id": entry_id, "axes": kept}


def _cost(entry):
    """The entry as a tuple where smaller is better on every position; missing is +inf."""
    axes = entry.get("axes") or {}
    out = []
    for name, direction in AXES:
        v = _number(axes.get(name))
        if v is None:
            out.append(math.inf)
        else:
            out.append(-v if direction == "max" else v)
    return tuple(out)


def dominates(a, b):
    """True when `a` is at least as good as `b` on every axis and strictly better on one."""
    ca, cb = _cost(a), _cost(b)
    return all(x <= y for x, y in zip(ca, cb)) and any(x < y for x, y in zip(ca, cb))


def _key(entry):
    e = make_entry(entry["id"], entry.get("axes"))
    return (json.dumps(e["axes"], sort_keys=True), e["id"])


def pareto_front(entries):
    """The entries no other entry dominates, de-duplicated and in canonical order."""
    norm = {}
    for e in entries:
        n = make_entry(e.get("id") if isinstance(e, dict) else None,
                       e.get("axes") if isinstance(e, dict) else None)
        norm.setdefault(_key(n), n)
    items = list(norm.values())
    front = [e for e in items if not any(dominates(o, e) for o in items if o is not e)]
    return sorted(front, key=_key)


def empty():
    return {"version": VERSION, "axes": [list(a) for a in AXES], "entries": []}


def insert(archive, entry):
    """A new archive holding the front of the old entries plus `entry`; `archive` is untouched."""
    old = (archive or {}).get("entries", [])
    new = make_entry(entry.get("id") if isinstance(entry, dict) else None,
                     entry.get("axes") if isinstance(entry, dict) else None)
    return {"version": VERSION, "axes": [list(a) for a in AXES],
            "entries": pareto_front(list(old) + [new])}


def build(entries):
    """An archive from many entries at once; the same as inserting them one by one."""
    return {"version": VERSION, "axes": [list(a) for a in AXES], "entries": pareto_front(entries)}


def to_json(archive):
    return json.dumps(archive, sort_keys=True, indent=2)


def from_json(text):
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ArchiveError("not JSON: %s" % exc)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ArchiveError("not an archive: no entries list")
    return build(data["entries"])


def entries_from_report(report):
    """Entries from a genotype_bench.run() report: one per genotype id (parent and candidates).

    success = mean score over the pools that measured; infra_failures = runs that errored. A
    candidate's own "axes" mapping (and the report's "parent_axes") overrides these and may add
    wall_time, turns, tool_calls, retries, regressions and human_interventions. A genotype with
    no ok run has no success axis."""
    if not isinstance(report, dict) or not isinstance(report.get("runs"), list):
        raise ArchiveError("not a bench report: no runs list")
    scores, errors, order = {}, {}, []
    for r in report["runs"]:
        gid = r.get("genotype")
        if not isinstance(gid, str) or not gid:
            continue
        if gid not in scores:
            scores[gid], errors[gid] = [], 0
            order.append(gid)
        s = _number(r.get("score"))
        if r.get("status") == "ok" and s is not None:
            scores[gid].append(s)
        else:
            errors[gid] += 1
    given = {}
    if isinstance(report.get("parent_axes"), dict) and isinstance(report.get("parent"), str):
        given[report["parent"]] = report["parent_axes"]
    for c in report.get("candidates") or []:
        if isinstance(c, dict) and isinstance(c.get("id"), str) and isinstance(c.get("axes"), dict):
            given[c["id"]] = c["axes"]
    out = []
    for gid in order:
        axes = {"infra_failures": errors[gid]}
        if scores[gid]:
            axes["success"] = sum(scores[gid]) / len(scores[gid])
        axes.update(given.get(gid, {}))
        out.append(make_entry(gid, axes))
    return out
