# -*- coding: utf-8 -*-
"""Read-only report: the fleet's base genotype, candidate validation, and how much data exists
to tell candidates apart (slice E1-a).

WHAT DECISION THIS SERVES. Whether a hand-written candidate organisation is valid and is a
different program from the base, and whether the recorded runs can already separate any two
organisations. It writes nothing (not into .fleet, not anywhere) and no runtime module imports
it, the same arrangement as scripts/capability_profile.py.

OUTPUT
  base        every gene, its live value, its allowed range and where it is read
  records     from .fleet/org_versions.jsonl: run lines, distinct (roles_hash, workflows_hash)
              pairs, and run lines that carry a genotype_id. With one pair, no two organisations
              have been recorded, so no comparison between them is possible yet.
  needed      runs per arm to see a success-rate difference of 0.15 (org_shadow_eval_report.
              runs_needed): 171 at base rate 0.5, measured on 2026-10-21, against the 22
              recorded runs.
  candidates  with --candidates FILE (JSON: one object, or a list, each {"genes": {...}}):
              VALID / NO-OP (same program as the base) / INVALID with the reason, and the genes
              that differ from the base.

Usage:  python scripts/org_genotype_report.py [--fleet-dir DIR] [--candidates FILE] [--format json]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.org import genotype  # noqa: E402


def _records(fleet_dir):
    """Counts from org_versions.jsonl. A missing file is empty; unreadable lines are counted."""
    path = os.path.join(fleet_dir, "org_versions.jsonl")
    runs, pairs, with_gid, bad = set(), set(), 0, 0
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
                if not isinstance(obj, dict) or obj.get("kind") != "run" or not obj.get("run_id"):
                    continue
                runs.add(str(obj["run_id"]))
                pairs.add((str(obj.get("roles_hash") or ""), str(obj.get("workflows_hash") or "")))
                if obj.get("genotype_id"):
                    with_gid += 1
    except OSError:
        pass
    return {"runs": len(runs), "distinct_org_versions": len(pairs),
            "runs_with_genotype_id": with_gid, "unreadable_lines": bad}


def _needed():
    from scripts.org_shadow_eval_report import runs_needed
    return {"delta": 0.15, "per_arm_at_base_rate_0.5": runs_needed(0.5),
            "per_arm_at_base_rate_0.7": runs_needed(0.7)}


def _load_candidates(path):
    with io.open(path, encoding="utf-8-sig") as fh:
        data = json.load(fh)
    return data if isinstance(data, list) else [data]


def check_candidate(cand, base_values):
    """{"status": VALID|NO-OP|INVALID, ...} for one candidate object."""
    genes = cand.get("genes") if isinstance(cand, dict) else None
    try:
        if genes is None:
            raise genotype.GenotypeError('a candidate needs a "genes" object')
        if genotype.same_program(genes, {}, base_values):
            return {"status": "NO-OP", "reason": "materialises to the base program"}
        return {"status": "VALID", "id": genotype.genotype_hash(genes, base_values),
                "differs": {k: list(v) for k, v in genotype.diff(genes, {}, base_values).items()}}
    except genotype.GenotypeError as exc:
        return {"status": "INVALID", "reason": str(exc)}


def build(fleet_dir, candidates_path=None):
    table = genotype.gene_table()
    base_values = genotype.current()
    base = {n: {"value": base_values[n], "allowed": list(g.allowed), "kind": g.kind,
                "read_by": g.source} for n, g in table.items()}
    out = {"base_org_id": genotype.base_org_id(), "base": base, "records": _records(fleet_dir),
           "needed": _needed()}
    if candidates_path:
        out["candidates"] = [check_candidate(c, base_values)
                             for c in _load_candidates(candidates_path)]
    return out


def _text(rep):
    lines = ["base organisation text id: %s" % (rep["base_org_id"] or "(unreadable)"), "", "base genes:"]
    for n, b in rep["base"].items():
        lines.append("  %-28s %-8s %-14s %s" % (n, b["value"], b["allowed"], b["read_by"]))
    r, nd = rep["records"], rep["needed"]
    lines += ["", "records: %d runs, %d distinct organisation versions, %d with genotype_id, "
              "%d unreadable lines" % (r["runs"], r["distinct_org_versions"],
                                       r["runs_with_genotype_id"], r["unreadable_lines"])]
    if r["distinct_org_versions"] < 2:
        lines.append("  fewer than 2 organisation versions recorded: no comparison is possible")
    lines.append("needed per arm for a 0.15 difference: %d at base rate 0.5, %d at 0.7"
                 % (nd["per_arm_at_base_rate_0.5"], nd["per_arm_at_base_rate_0.7"]))
    for i, c in enumerate(rep.get("candidates", [])):
        extra = c.get("reason") or "id %s differs %s" % (c.get("id"), c.get("differs"))
        lines.append("candidate %d: %s  %s" % (i, c["status"], extra))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fleet-dir", default=os.path.join(REPO, ".fleet"))
    ap.add_argument("--candidates", default=None)
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    rep = build(args.fleet_dir, args.candidates)
    if args.format == "json":
        text = json.dumps(rep, ensure_ascii=False, indent=2)
    else:
        text = _text(rep)
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
