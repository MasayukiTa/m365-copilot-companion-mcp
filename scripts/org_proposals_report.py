# -*- coding: utf-8 -*-
"""Read-only report: the change proposals for role and protocol definitions that are on file (D3).

WHAT DECISION THIS SERVES. Which proposed prompt changes are waiting for a person. Each line comes
from .fleet/org_proposals.jsonl (written by the propose_org_change tool when the setting
`org_proposals` is on). The report also shows the state of each proposal's decision card in the
pending queue (open / approved / done / dropped, or none) and whether the definition has changed
since the proposal was made (the base hash no longer matches), in which case it is stale.

It writes nothing and applies nothing. Applying a proposal is a separate step a person approves,
and it is not implemented.

  python scripts/org_proposals_report.py [--fleet-dir .fleet] [--format text|json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def build(path=None):
    """{"proposals": [...], "unreadable": n, "count": n}. Each proposal carries `card` (its
    decision-card status or "") and `stale` (the definition's hash is no longer its base)."""
    from relay.org import proposals
    rows, bad = proposals.listing(path)
    try:
        now = proposals.targets()
    except Exception:
        now = {}
    for r in rows:
        r["stale"] = now.get(r["target"], "") != r["base_hash"]
    return {"proposals": rows, "unreadable": bad, "count": len(rows)}


def render_text(rep):
    lines = ["%d change proposal(s) on file; %d unreadable line(s)" % (rep["count"], rep["unreadable"])]
    for r in rep["proposals"]:
        lines.append("- %s  %s -> %s  by %s  card=%s%s" % (
            r["target"], r["base_hash"], r["proposed_hash"], r["author"], r["card"] or "none",
            "  STALE (definition changed since)" if r["stale"] else ""))
        lines.append("    " + r["rationale"])
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fleet-dir", default=os.path.join(REPO, ".fleet"))
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    from relay.org import proposals
    rep = build(os.path.join(args.fleet_dir, proposals.FILENAME))
    out = json.dumps(rep, ensure_ascii=False, indent=1) + "\n" if args.format == "json" \
        else render_text(rep)
    sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
