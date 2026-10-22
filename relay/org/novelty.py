# -*- coding: utf-8 -*-
"""Population novelty and quality-diversity report (slice G4, report only).

WHAT DECISION THIS SERVES. Whether a population of org-agent records is still exploring or has
collapsed onto copies of one design: which members are far from everything else (novel), which are
copies of each other (duplicate clusters), and whether each niche has a best member and how good it
is. It returns a report and nothing else: it writes no file (not under .fleet either), deletes
nothing, calls no network, no fleet and no model, runs no process and has no clock.

Default off: no runtime module imports it, only scripts/org_novelty.py, run by hand.

RECORD FIELDS READ (every one optional, absent stays absent).
  genotype_id | agent_id | id   the member id (first non-empty string wins)
  parts                         {label: text}, the shape relay/org/def_store.py stores
  genes                         {name: scalar}, typed gene values
  behavior                      {name: number}, measured behavioural descriptors
  quality                       a real number (bool and NaN are not numbers)
  niche                         a string label of the niche the member occupies

DISTANCES (both are in [0, 1]; a distance that cannot be computed is None, never 0).
  structural   Jaccard distance between the feature sets of two members. A gene contributes the
               feature (name, value); a part contributes one feature per (label, word) of its text,
               words split on whitespace. A member with no features has no structural distance.
               Two members with both feature sets empty are not compared.
  behavioral   mean absolute difference over the behaviour names both members carry, each name
               scaled by its min..max range over the whole population (a name constant across the
               population contributes 0). No shared name: no behavioural distance.

NOVELTY. The novelty of a member is the mean distance to its k nearest neighbours (k = min(K, the
number of members with a computable distance to it)); K defaults to 5 and is an argument, not a
measurement. Structural and behavioural novelty are reported separately and are not combined: no
measurement gives a weight between them. A member with no computable distance has novelty None.

DUPLICATE CLUSTERS. Members whose structural distance is <= `near` (default 0.0, exact feature-set
equality) are joined transitively. Clusters of two or more are listed, largest first. A cluster
under a positive `near` can chain members that are further than `near` from each other; the report
carries the largest pairwise distance inside each cluster so that is visible.

QUALITY-DIVERSITY. Per niche: members, members with a quality, the best quality and the id that has
it (ties go to the smaller id). Members without a niche go to the label `unassigned`, which is
listed but never counted as a covered niche. qd_score is the sum of the per-niche best qualities
over the niches that have one. Coverage is covered_niches / len(declared) when the caller declares
the niche list, else None (the total number of niches is not known from the records).

A row that is not an object, has no id, or repeats an earlier id is skipped; it is counted and its
index and code are listed. Nothing is invented for it.
"""
from __future__ import annotations

import json
import math

VERSION = 1
DEFAULT_K = 5
ID_KEYS = ("genotype_id", "agent_id", "id")
UNASSIGNED = "unassigned"


def _num(value):
    """A finite real number, or None. bool is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return float(value)


def member_id_of(record):
    """The member id of a record, or None."""
    if not isinstance(record, dict):
        return None
    for key in ID_KEYS:
        v = record.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def features_of(record):
    """The structural feature set of a record (a frozenset, possibly empty)."""
    feats = set()
    genes = record.get("genes")
    if isinstance(genes, dict):
        for name, value in genes.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                feats.add(("g", str(name), json.dumps(value, sort_keys=True)))
    parts = record.get("parts")
    if isinstance(parts, dict):
        for label, text in parts.items():
            if isinstance(text, str):
                for word in text.split():
                    feats.add(("p", str(label), word))
    return frozenset(feats)


def behavior_of(record):
    """{name: float} of the numeric behaviour descriptors."""
    b = record.get("behavior")
    if not isinstance(b, dict):
        return {}
    out = {}
    for name, value in b.items():
        n = _num(value)
        if n is not None:
            out[str(name)] = n
    return out


def structural_distance(a, b):
    """Jaccard distance of two feature sets, or None when both are empty."""
    union = len(a | b)
    if union == 0:
        return None
    return 1.0 - len(a & b) / union


def behavioral_distance(a, b, ranges):
    """Mean scaled absolute difference over shared names, or None when none is shared."""
    shared = sorted(set(a) & set(b))
    if not shared:
        return None
    total = 0.0
    for name in shared:
        lo, hi = ranges[name]
        total += 0.0 if hi == lo else abs(a[name] - b[name]) / (hi - lo)
    return total / len(shared)


def _members(records):
    rows = records if isinstance(records, list) else []
    members, skipped, seen = [], [], set()
    for index, rec in enumerate(rows):
        mid = member_id_of(rec)
        if mid is None:
            skipped.append({"index": index, "code": "malformed"})
            continue
        if mid in seen:
            skipped.append({"index": index, "code": "duplicate_id", "id": mid})
            continue
        seen.add(mid)
        niche = rec.get("niche")
        niche = niche.strip() if isinstance(niche, str) and niche.strip() else None
        members.append({"id": mid, "features": features_of(rec), "behavior": behavior_of(rec),
                        "quality": _num(rec.get("quality")), "niche": niche, "index": index})
    members.sort(key=lambda m: m["id"])
    return len(rows), members, skipped


def _ranges(members):
    ranges = {}
    for m in members:
        for name, v in m["behavior"].items():
            lo, hi = ranges.get(name, (v, v))
            ranges[name] = (min(lo, v), max(hi, v))
    return ranges


def _novelty(dist_rows, k):
    """Mean of the k smallest computable distances, or None when there is none."""
    ds = sorted(d for d in dist_rows if d is not None)
    if not ds:
        return None
    take = ds[:max(1, min(int(k), len(ds)))]
    return sum(take) / len(take)


def _clusters(members, structural, near):
    parent = list(range(len(members)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    n = len(members)
    for i in range(n):
        for j in range(i + 1, n):
            d = structural[i][j]
            if d is not None and d <= near:
                parent[find(i)] = find(j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    out = []
    for idx in groups.values():
        if len(idx) < 2:
            continue
        widest = max(structural[a][b] for a in idx for b in idx if a < b
                     and structural[a][b] is not None)
        out.append({"ids": [members[i]["id"] for i in idx], "size": len(idx),
                    "max_pairwise_distance": widest})
    out.sort(key=lambda c: (-c["size"], c["ids"]))
    return out


def _qd(members, declared):
    niches = {}
    for m in members:
        label = m["niche"] or UNASSIGNED
        e = niches.setdefault(label, {"members": 0, "with_quality": 0, "best_quality": None,
                                      "best_id": None})
        e["members"] += 1
        q = m["quality"]
        if q is None:
            continue
        e["with_quality"] += 1
        if e["best_quality"] is None or q > e["best_quality"] or (
                q == e["best_quality"] and m["id"] < e["best_id"]):
            e["best_quality"], e["best_id"] = q, m["id"]
    covered = sorted(n for n, e in niches.items() if n != UNASSIGNED and e["members"])
    scored = [e["best_quality"] for n, e in niches.items()
              if n != UNASSIGNED and e["best_quality"] is not None]
    decl = sorted({d.strip() for d in (declared or []) if isinstance(d, str) and d.strip()})
    coverage = None
    missing = []
    if decl:
        hit = [d for d in decl if d in niches and niches[d]["members"]]
        coverage = len(hit) / len(decl)
        missing = [d for d in decl if d not in hit]
    return {"niches": {n: niches[n] for n in sorted(niches)}, "covered_niches": covered,
            "qd_score": sum(scored), "declared_niches": decl, "coverage": coverage,
            "missing_niches": missing}


def build_report(records, k=DEFAULT_K, near=0.0, niches=None):
    """Novelty per member, duplicate clusters and the quality-diversity summary. Pure."""
    total, members, skipped = _members(records)
    ranges = _ranges(members)
    n = len(members)
    structural = [[None] * n for _ in range(n)]
    behavioral = [[None] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            s = structural_distance(members[i]["features"], members[j]["features"])
            b = behavioral_distance(members[i]["behavior"], members[j]["behavior"], ranges)
            structural[i][j] = structural[j][i] = s
            behavioral[i][j] = behavioral[j][i] = b
    out = {}
    for i, m in enumerate(members):
        out[m["id"]] = {
            "structural_novelty": _novelty([structural[i][j] for j in range(n) if j != i], k),
            "behavioral_novelty": _novelty([behavioral[i][j] for j in range(n) if j != i], k),
            "niche": m["niche"], "quality": m["quality"], "record_index": m["index"]}
    clusters = _clusters(members, structural, near)
    in_cluster = sum(c["size"] for c in clusters)
    qd = _qd(members, niches)
    return {"version": VERSION, "total": total, "malformed": len(skipped), "members": n,
            "k": k, "near": near, "members_in_duplicate_clusters": in_cluster,
            "duplicate_clusters": clusters, "quality_diversity": qd, "novelty": out,
            "skipped": skipped}


def to_json(report):
    """Stable text form of a report."""
    return json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
