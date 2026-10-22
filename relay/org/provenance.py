# -*- coding: utf-8 -*-
"""Data provenance and contamination report (slice G2, report only).

WHAT DECISION THIS SERVES. Whether a benchmark number may be cited as an estimate of generalisation:
a sealed holdout item that was also used for design or debugging, or whose id or text reached the
inputs that generated a candidate, is no longer held out. This module reads run records and says,
per item, which pools it was used in and which contamination flags apply. It returns a report and
nothing else: it writes no file (not under .fleet either), deletes nothing, calls no network, no
fleet and no model, runs no process and has no clock.

Default off: no runtime module imports it, only scripts/org_provenance.py, run by hand.

RECORD FIELDS READ (the names relay/selfimprove/episode_record.py uses, plus the item id and the
candidate-generation inputs that a run log may carry; every one is optional, absent stays absent).
  item_id | instance_id | task_id   the benchmark item (first non-empty string wins)
  pool                              which pool the run read. "sealed" is the held-out pool
                                    (episode_record.GENERALISATION_POOLS). Any other non-empty pool
                                    (dev, regression, train, ...) is the optimiser's own feedback.
  burned                            true marks the item as already seen for design or debugging,
                                    whatever pool the run names (burned.jsonl rows use this meaning)
  item_text                         the text of the item, when the run recorded it
  candidate_inputs                  a string or a list of strings that were given to the step that
                                    generated a candidate (a mutation prompt, a diagnosis, a note)

ROLES. holdout = pool "sealed" (case-insensitive; "holdout" is accepted as a synonym).
design = any other non-empty pool, or burned true. A record naming no pool and not burned is
`unknown` and is counted, never assumed to be either.

FLAGS (an item can carry several).
  reused_across_train_and_holdout   the item has at least one design use and at least one holdout use
  holdout_id_in_candidate_inputs    a holdout item's id appears, as a whole token, in the
                                    candidate_inputs of any record
  holdout_text_in_candidate_inputs  a holdout item's item_text (after trimming) appears in the
                                    candidate_inputs of any record
  burned_marked                     the item has a record with burned true
An id token is bounded by characters that are not letters, digits, underscore or hyphen, so item
"a-1" is not found inside "a-10". Text matching is a plain substring test on the trimmed text with
case kept: it finds copies, not paraphrases, and the report says so rather than claiming more.

A row that is not an object, or has no item id, is `malformed` and skipped; it is counted and its
index is listed. Nothing is invented for it.
"""
from __future__ import annotations

import json
import re

VERSION = 1
HOLDOUT, DESIGN, UNKNOWN = "holdout", "design", "unknown"
HOLDOUT_POOLS = frozenset({"sealed", "holdout"})
ID_KEYS = ("item_id", "instance_id", "task_id")
REUSED, ID_LEAK, TEXT_LEAK, BURNED = (
    "reused_across_train_and_holdout", "holdout_id_in_candidate_inputs",
    "holdout_text_in_candidate_inputs", "burned_marked")
FLAGS = (REUSED, ID_LEAK, TEXT_LEAK, BURNED)


def item_id_of(record):
    """The item id of a record, or None."""
    if not isinstance(record, dict):
        return None
    for key in ID_KEYS:
        v = record.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def role_of(record):
    """HOLDOUT, DESIGN or UNKNOWN for one record."""
    if not isinstance(record, dict):
        return UNKNOWN
    pool = record.get("pool")
    pool = pool.strip().lower() if isinstance(pool, str) else ""
    if pool in HOLDOUT_POOLS:
        return HOLDOUT
    if pool or record.get("burned") is True:
        return DESIGN
    return UNKNOWN


def _inputs(record):
    """The candidate-generation input strings of a record (non-string entries are ignored)."""
    v = record.get("candidate_inputs")
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    return [s for s in v if isinstance(s, str) and s]


def _id_pattern(item_id):
    return re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(item_id) + r"(?![A-Za-z0-9_-])")


def build_provenance(records):
    """Per item provenance: {item_id: {...}} plus the skipped rows. Pure."""
    rows = records if isinstance(records, list) else []
    items, skipped = {}, []
    for index, rec in enumerate(rows):
        iid = item_id_of(rec)
        if iid is None:
            skipped.append({"index": index, "code": "malformed"})
            continue
        it = items.setdefault(iid, {"item_id": iid, "holdout_uses": 0, "design_uses": 0,
                                    "unknown_uses": 0, "pools": set(), "burned_marked": False,
                                    "texts": set(), "indexes": []})
        role = role_of(rec)
        it[{HOLDOUT: "holdout_uses", DESIGN: "design_uses", UNKNOWN: "unknown_uses"}[role]] += 1
        pool = rec.get("pool")
        if isinstance(pool, str) and pool.strip():
            it["pools"].add(pool.strip().lower())
        if rec.get("burned") is True:
            it["burned_marked"] = True
        text = rec.get("item_text")
        if isinstance(text, str) and text.strip():
            it["texts"].add(text.strip())
        it["indexes"].append(index)
    return items, skipped


def build_report(records):
    """Provenance per item and the contamination counts. Pure."""
    rows = records if isinstance(records, list) else []
    items, skipped = build_provenance(rows)
    inputs = [s for rec in rows if isinstance(rec, dict) for s in _inputs(rec)]
    joined = "\n".join(inputs)
    out_items = {}
    for iid in sorted(items):
        it = items[iid]
        flags = []
        if it["holdout_uses"] and it["design_uses"]:
            flags.append(REUSED)
        if it["holdout_uses"] and inputs and _id_pattern(iid).search(joined):
            flags.append(ID_LEAK)
        if it["holdout_uses"] and any(t in joined for t in sorted(it["texts"])):
            flags.append(TEXT_LEAK)
        if it["burned_marked"]:
            flags.append(BURNED)
        out_items[iid] = {"holdout_uses": it["holdout_uses"], "design_uses": it["design_uses"],
                          "unknown_uses": it["unknown_uses"], "pools": sorted(it["pools"]),
                          "burned_marked": it["burned_marked"], "flags": flags,
                          "record_indexes": it["indexes"]}
    flag_counts = {f: sum(1 for v in out_items.values() if f in v["flags"]) for f in FLAGS}
    contaminated = sorted(i for i, v in out_items.items() if any(f != BURNED for f in v["flags"]))
    holdout_items = [i for i, v in out_items.items() if v["holdout_uses"]]
    return {"version": VERSION, "total": len(rows), "malformed": len(skipped),
            "items": len(out_items),
            "holdout_items": len(holdout_items),
            "design_items": sum(1 for v in out_items.values() if v["design_uses"]),
            "unknown_only_items": sum(1 for v in out_items.values()
                                      if not v["holdout_uses"] and not v["design_uses"]),
            "flag_counts": flag_counts,
            "contaminated_holdout_items": [i for i in contaminated if out_items[i]["holdout_uses"]],
            "clean_holdout_items": sorted(i for i in holdout_items if i not in contaminated),
            "provenance": out_items, "skipped": skipped}


def to_json(report):
    """Stable text form of a report."""
    return json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
