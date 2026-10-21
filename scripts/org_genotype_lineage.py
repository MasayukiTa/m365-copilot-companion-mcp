# -*- coding: utf-8 -*-
"""Read-only report: the lineage of the recorded organisation genotypes (slice E1-c).

WHAT DECISION THIS SERVES. Which recorded candidates descend from which, what each one changed
relative to its parent, and whether the ledger's parent links are sound (a parent that was never
recorded, a link that loops). It reads `<fleet dir>/org_genotypes.jsonl` (relay/org/genotype_ledger)
and writes nothing; no runtime module imports it, the same arrangement as
scripts/org_genotype_report.py.

OUTPUT, per recorded genotype (file order): id, parent, depth (0 for a root), the number of
direct children, and `changed`: the genes whose value differs from the parent's recorded values
([parent value, own value]). A root has no parent in the ledger, so `changed` is empty for it.
`parent_state` is one of: root (no parent named), linked (parent is recorded), missing (a parent
is named but is not in the ledger), cycle (following parents returns to the row itself or loops).
Unreadable ledger lines are counted, not hidden.

Usage:  python scripts/org_genotype_lineage.py [--fleet-dir DIR] [--format json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def _state(eid, by_id):
    """(parent_state, depth). depth counts recorded ancestors; None when the chain is not sound."""
    seen, cur, depth = {eid}, by_id[eid].get("parent") or "", 0
    if not cur:
        return "root", 0
    while cur:
        if cur not in by_id:
            return "missing", None
        if cur in seen:
            return "cycle", None
        seen.add(cur)
        depth += 1
        cur = by_id[cur].get("parent") or ""
    return "linked", depth


def build(entries, unreadable=0):
    """The lineage report for ledger rows `entries` (ledger.load order = file order)."""
    by_id = {}
    for e in entries:
        by_id.setdefault(e["id"], e)  # the ledger never writes an id twice; keep the first
    children = {}
    for e in by_id.values():
        p = e.get("parent") or ""
        if p:
            children[p] = children.get(p, 0) + 1
    rows = []
    for eid, e in by_id.items():
        state, depth = _state(eid, by_id)
        parent = e.get("parent") or ""
        changed = {}
        if parent in by_id and parent != eid:
            pg, g = by_id[parent].get("genes", {}), e.get("genes", {})
            changed = {k: [pg.get(k), g.get(k)] for k in sorted(set(pg) | set(g))
                       if pg.get(k) != g.get(k)}
        rows.append({"id": eid, "parent": parent, "parent_state": state, "depth": depth,
                     "children": children.get(eid, 0), "changed": changed})
    states = [r["parent_state"] for r in rows]
    return {"count": len(rows), "unreadable_lines": unreadable,
            "roots": states.count("root"), "missing_parent": states.count("missing"),
            "cycles": states.count("cycle"), "rows": rows}


def _text(rep):
    lines = ["%d recorded genotypes: %d roots, %d with a missing parent, %d in a cycle, "
             "%d unreadable ledger lines" % (rep["count"], rep["roots"], rep["missing_parent"],
                                             rep["cycles"], rep["unreadable_lines"])]
    for r in rep["rows"]:
        what = ", ".join("%s %s->%s" % (k, v[0], v[1]) for k, v in r["changed"].items())
        lines.append("  %s  parent=%s (%s) depth=%s children=%d  %s" % (
            r["id"], r["parent"] or "-", r["parent_state"],
            "-" if r["depth"] is None else r["depth"], r["children"], what))
    return "\n".join(lines)


def main(argv=None):
    from relay.org import genotype_ledger as L
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fleet-dir", default=os.path.join(REPO, ".fleet"))
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    entries, bad = L.load(L.ledger_path(args.fleet_dir))
    rep = build(entries, bad)
    text = json.dumps(rep, ensure_ascii=False, indent=2) if args.format == "json" else _text(rep)
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
