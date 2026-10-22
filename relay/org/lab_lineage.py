# -*- coding: utf-8 -*-
"""Strict lineage check for Evolution Lab experiments (slice F3).

WHAT DECISION THIS SERVES. Whether a set of lab candidates can be trusted as one family tree, so
that "this candidate came from that one" is a fact a later promotion or archive step may rely on.
The multi-objective archive (relay/org/genotype_archive.py) keys entries by an id, and the
genotype module (relay/org/genotype.py) derives a content hash for each genotype and records a
`parent`. If two experiments share an id, a parent points at nothing, or a chain loops back on
itself, any "improved over its parent" claim compares the wrong things or never terminates. This
module refuses such a set before anything is promoted. Nothing here decides promotion.

WHAT IT DOES. check_lineage() takes experiment records and returns a deterministic list of
violations. It is pure: it reads no file, writes no file, runs no git, starts no process and has
no clock (a caller that wants an age check passes `now` itself; this module needs none). Default
off: nothing in production calls it, only scripts/org_lab_lineage.py, run by hand with --check.

RECORD: {"experiment_id", "candidate_id", "parent_ids": [...], "genotype_hash", "created_at"
(ISO 8601), "state"}. One record is one experiment run on one candidate. `parent_ids` names
candidate ids, or the literal root marker "baseline" (the unmodified production definition, which
is not a candidate and has no record). A record with no parents, or only "baseline", is a root.

ID SHAPES. experiment_id is "exp-YYYYMMDD-" followed by 6 or more lowercase hex digits
(exp-20261022-a1b2c3). The date part makes ids sort in creation order, and the hex part is the
same lowercase hex alphabet the genotype hash uses (genotype.py HASH_LEN is 16 hex digits taken
from a sha256). genotype_hash here is the FULL sha256 hex (64 chars): the 16-digit form used by
genotype.py is a prefix of it, so a lab record that keeps the full digest can always be matched
to an archive entry id by prefix, while a truncated value in a lineage record could not be told
from a collision.

RULES (each is a stable code; the reason is the line under it).
  duplicate_experiment_id
      two records share an experiment_id. Reason: the id is the join key to results; a repeat
      makes one result stand for two runs.
  duplicate_candidate_id
      two records share a candidate_id. Reason: parent_ids name candidates, so a repeat makes
      "the parent" ambiguous.
  experiment_id_format
      experiment_id is present but does not match ^exp-[0-9]{8}-[0-9a-f]{6,}$. Reason: ids that
      cannot be sorted or matched by pattern are the ones that get typed by hand and mistyped.
  genotype_hash_format
      genotype_hash is not 64 lowercase hex digits. Reason: see ID SHAPES; a short or mixed-case
      value cannot be matched to the archive reliably.
  orphan_experiment
      experiment_id is missing or empty. Reason: a candidate that belongs to no experiment has
      no recorded purpose or result, so it can never be explained later.
  self_parent
      a candidate lists itself in parent_ids. Reason: a self edge is the smallest cycle and is
      reported on its own so it is not hidden inside a cycle report.
  missing_parent
      a parent id is neither a candidate_id in the set nor "baseline". Reason: the chain is cut
      and the origin of the candidate is unknown.
  cycle
      following parent_ids leads back to a candidate already on the path (reported once per
      cycle, on the smallest candidate_id in it). Reason: a loop has no root, so depth and
      ancestry are undefined.
  timestamp_order
      a child's created_at is earlier than a parent's. Reason: a child cannot be made from
      something that did not exist yet; an inverted pair means a wrong parent or a wrong clock.

created_at that is not ISO 8601, or a record that is not an object with the listed keys of the
right type, is refused with LabLineageError (bad input, not a lineage finding).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

VERSION = 1
ROOT_MARKER = "baseline"
STATES = ("active", "promoted", "rejected", "merged")
EXPERIMENT_ID_RE = re.compile(r"^exp-[0-9]{8}-[0-9a-f]{6,}$")
GENOTYPE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class LabLineageError(ValueError):
    """An input record or timestamp is not usable."""


def parse_time(value, what="timestamp"):
    """ISO 8601 text -> aware datetime (naive input counts as UTC; a trailing Z is accepted)."""
    if not isinstance(value, str) or not value.strip():
        raise LabLineageError("%s must be a non-empty ISO 8601 string" % what)
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise LabLineageError("%s is not ISO 8601: %r" % (what, value))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalize_record(rec):
    """Validate the shape of one record; returns a clean copy with a parsed `created`."""
    if not isinstance(rec, dict):
        raise LabLineageError("a lineage record must be an object")
    cid = rec.get("candidate_id")
    if not isinstance(cid, str) or not cid.strip():
        raise LabLineageError("candidate_id must be a non-empty string")
    out = {"candidate_id": cid}
    for key in ("experiment_id", "genotype_hash"):
        v = rec.get(key)
        if v is None:
            v = ""
        if not isinstance(v, str):
            raise LabLineageError("record %r: %s must be a string" % (cid, key))
        out[key] = v
    parents = rec.get("parent_ids", [])
    if not isinstance(parents, list) or not all(isinstance(p, str) and p for p in parents):
        raise LabLineageError("record %r: parent_ids must be a list of non-empty strings" % cid)
    out["parent_ids"] = list(parents)
    if rec.get("state") not in STATES:
        raise LabLineageError("record %r: state must be one of %s" % (cid, "/".join(STATES)))
    out["state"] = rec["state"]
    out["created_at"] = rec.get("created_at")
    out["created"] = parse_time(rec.get("created_at"), "record %r created_at" % cid)
    return out


def _normalize_all(records):
    if not isinstance(records, list):
        raise LabLineageError("records must be a list")
    return sorted((normalize_record(r) for r in records),
                  key=lambda r: (r["created"], r["candidate_id"], r["experiment_id"]))


def _by_candidate(recs):
    index = {}
    for r in recs:
        index.setdefault(r["candidate_id"], r)  # first (earliest) wins; duplicates are reported
    return index


def _real_parents(rec, index):
    """Parents that exist as candidates, excluding the root marker and a self edge."""
    return [p for p in rec["parent_ids"]
            if p != ROOT_MARKER and p != rec["candidate_id"] and p in index]


def _find_cycles(index):
    """Cycles as sorted candidate-id tuples, each once. Deterministic (ids visited in order)."""
    state, cycles = {}, set()

    def visit(cid, path):
        state[cid] = 1
        path.append(cid)
        for p in sorted(_real_parents(index[cid], index)):
            if state.get(p) == 1:
                cycles.add(tuple(sorted(path[path.index(p):])))
            elif p not in state:
                visit(p, path)
        path.pop()
        state[cid] = 2

    for cid in sorted(index):
        if cid not in state:
            visit(cid, [])
    return sorted(cycles)


def check_lineage(records):
    """Deterministic report: {"version", "ok", "count", "violations": [{code, id, detail}]}.

    `id` is the candidate_id the finding is about (or the repeated id for duplicates).
    """
    recs = _normalize_all(records)
    index = _by_candidate(recs)
    v = []

    def add(code, rid, detail):
        v.append({"code": code, "id": rid, "detail": detail})

    for key, code in (("experiment_id", "duplicate_experiment_id"),
                      ("candidate_id", "duplicate_candidate_id")):
        seen = {}
        for r in recs:
            if r[key]:
                seen[r[key]] = seen.get(r[key], 0) + 1
        for value in sorted(k for k, n in seen.items() if n > 1):
            add(code, value, "%d records have %s %r" % (seen[value], key, value))
    for r in recs:
        cid = r["candidate_id"]
        if not r["experiment_id"]:
            add("orphan_experiment", cid, "candidate belongs to no experiment")
        elif not EXPERIMENT_ID_RE.match(r["experiment_id"]):
            add("experiment_id_format", cid, "experiment_id %r does not match %s"
                % (r["experiment_id"], EXPERIMENT_ID_RE.pattern))
        if not GENOTYPE_HASH_RE.match(r["genotype_hash"]):
            add("genotype_hash_format", cid, "genotype_hash is not 64 lowercase hex digits")
        for p in r["parent_ids"]:
            if p == cid:
                add("self_parent", cid, "candidate lists itself as a parent")
            elif p != ROOT_MARKER and p not in index:
                add("missing_parent", cid, "parent %r is not a candidate and not %r"
                    % (p, ROOT_MARKER))
            elif p != ROOT_MARKER and index[p]["created"] > r["created"]:
                add("timestamp_order", cid, "created before its parent %r" % p)
    for cyc in _find_cycles(index):
        add("cycle", cyc[0], "parent chain loops through %s" % ", ".join(cyc))
    return {"version": VERSION, "ok": not v, "count": len(recs), "violations": v}


def _depths(index):
    """Longest root-to-node chain length per candidate (a root is 1). Cycle edges are ignored."""
    memo, visiting = {}, set()

    def depth(cid):
        if cid in memo:
            return memo[cid]
        visiting.add(cid)
        best = 0
        for p in _real_parents(index[cid], index):
            if p not in visiting:
                best = max(best, depth(p))
        visiting.discard(cid)
        memo[cid] = best + 1
        return memo[cid]

    for cid in sorted(index):
        depth(cid)
    return memo


def lineage_of(records, candidate_id):
    """Ancestor chain of a candidate: list of candidate ids, root first, the candidate last.

    The root marker is not included. With several parents every ancestor appears once, ordered
    by (chain depth, candidate_id), so the output is deterministic. Unknown ids are refused.
    """
    index = _by_candidate(_normalize_all(records))
    if candidate_id not in index:
        raise LabLineageError("unknown candidate_id %r" % (candidate_id,))
    depth = _depths(index)
    seen, stack = set(), [candidate_id]
    while stack:
        cid = stack.pop()
        if cid in seen:
            continue
        seen.add(cid)
        stack.extend(_real_parents(index[cid], index))
    return sorted(seen, key=lambda c: (depth[c], c)) if len(seen) > 1 else [candidate_id]


def summarize(records):
    """Deterministic shape of the tree: depth, roots, branching.

    depth: longest chain, counting records (a lone root is 1). roots: records with no parent
    candidate (no parents, only the root marker, or only missing parents). max_branching: most
    children any candidate has. mean_branching: children per candidate that has any, rounded to
    3 places (0.0 when nobody has children).
    """
    index = _by_candidate(_normalize_all(records))
    depth = _depths(index)
    children = {}
    for cid, r in index.items():
        for p in set(_real_parents(r, index)):
            children[p] = children.get(p, 0) + 1
    roots = sorted(c for c, r in index.items() if not _real_parents(r, index))
    counts = list(children.values())
    return {"version": VERSION, "count": len(index),
            "depth": max(depth.values()) if depth else 0,
            "roots": len(roots), "root_ids": roots,
            "max_branching": max(counts) if counts else 0,
            "mean_branching": round(sum(counts) / len(counts), 3) if counts else 0.0}


def to_json(obj):
    """Stable JSON text for a report."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, indent=2)
