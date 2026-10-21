# -*- coding: utf-8 -*-
"""Append-only record of the organisation genotypes someone wrote down (slice E1-b, record-only).

WHAT DECISION THIS SERVES. Which candidate organisations have been defined, from which parent,
and which of them are the same program, so that later slices have a list to evaluate instead of
JSON files scattered on a desktop. It evaluates nothing, applies nothing, and no run reads it.

THE LEDGER, `<fleet dir>/org_genotypes.jsonl`, one JSON object per line:
  {"kind":"genotype", "ts", "id", "archive_id", "base", "parent", "genes", "schema_version"}
`id` is relay/org/genotype's hash of the filled-in gene values; `archive_id` is the id the
selfimprove archive gives the same content (archive.genome_id with the "org" key), so a row here
and a row in an archive can be joined. No goal text and no prompt text.

WHERE IT LIVES. The caller names the directory (as relay/org/promotion does); no path to the
operator's live state is fixed here, and only scripts/org_genotype_record.py (run by a person)
calls it. The tracked archive relay/selfimprove/archive/entries.jsonl is never written: it is
published, and this record is not.

A genotype whose id is already in the ledger is not written a second time (an A/A under two
names stays one row). NO SIZE CAP: lines are written by a person, and no measurement of growth
exists, so a cap would have no recorded basis.

FAILURE-SAFE. Functions return a result dict instead of raising; the ledger is never rewritten;
a line this version does not know is skipped so an older reader tolerates a newer writer.
"""
from __future__ import annotations

import io
import json
import os
import threading
import time

FILENAME = "org_genotypes.jsonl"
KIND = "genotype"

_LOCK = threading.Lock()


def ledger_path(fleet_dir) -> str:
    return os.path.join(os.path.abspath(str(fleet_dir)), FILENAME)


def load(path):
    """(entries, unreadable). Read-only; a missing file is empty. Unknown kinds are skipped
    without being counted; a malformed line or one missing id/genes/ts is counted."""
    entries, bad = [], 0
    try:
        with io.open(path, encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if not isinstance(obj, dict):
                    bad += 1
                    continue
                if obj.get("kind") != KIND:
                    continue
                if (obj.get("id") and isinstance(obj.get("genes"), dict)
                        and isinstance(obj.get("ts"), (int, float))):
                    entries.append(obj)
                else:
                    bad += 1
    except OSError:
        pass
    return entries, bad


def archive_id(genes) -> str:
    """The id the selfimprove archive gives these gene values (the "org" key of a genome)."""
    from relay.selfimprove import archive
    return archive.genome_id({"org": dict(genes)})


def record(path, genes, parent="", base=None, now=None) -> dict:
    """Validate and append one candidate. Refused, writing nothing, when it is invalid, when it
    materialises to the same program as the base (a no-op), or when its id is already recorded.
    `base` defaults to the live values. Applies nothing."""
    try:
        from relay.org import genotype
        base_values = dict(base) if base is not None else genotype.current()
        if genotype.same_program(genes or {}, {}, base_values):
            return {"status": "refused", "reason": "the same program as the base"}
        rec = genotype.genotype(genes or {}, parent=parent, base=base_values)
        with _LOCK:
            known = {e["id"] for e in load(path)[0]}
            if rec["id"] in known:
                return {"status": "refused", "reason": "already recorded", "id": rec["id"]}
            row = {"kind": KIND, "ts": float(time.time() if now is None else now),
                   "id": rec["id"], "archive_id": archive_id(rec["genes"]), "base": rec["base"],
                   "parent": rec["parent"], "genes": rec["genes"],
                   "schema_version": rec["schema_version"]}
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with io.open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        return {"status": "recorded", "id": rec["id"], "archive_id": row["archive_id"]}
    except Exception as exc:
        from relay.org import genotype as _g
        if isinstance(exc, _g.GenotypeError):
            return {"status": "refused", "reason": str(exc)}
        return {"status": "error", "reason": "the genotype could not be recorded"}
