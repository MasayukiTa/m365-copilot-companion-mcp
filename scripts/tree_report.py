"""Read-only markdown report on the task tree held in a `.fleet` directory.

    python scripts/tree_report.py [--fleet-dir DIR] [--json]

Reads `status.json` and `campaigns.jsonl` and prints a markdown report: tree size, depth,
descendants and subtree turns per root, retry/duplication rate (workers per goal_hash and per
jid), merge failure rate, quota pressure when the status file carries it, and an explicit
"unknown" bucket for rows the tree cannot place or read. Nothing is written anywhere.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.family_view import read_fleet_dir  # noqa: E402
from relay.task_tree import build_tree, tree_shape  # noqa: E402

TOP_ROOTS = 10
_TERMINAL = ("done", "failed", "interrupted")


def _pct(n, d):
    return "n/a (0 rows)" if not d else "%d/%d (%.1f%%)" % (n, d, 100.0 * n / d)


def _duplication(workers, key):
    """(rows carrying the key, distinct values, rows without it)."""
    vals = [str(w.get(key)) for w in workers if isinstance(w, dict) and w.get(key) not in (None, "")]
    missing = sum(1 for w in workers if isinstance(w, dict) and w.get(key) in (None, ""))
    return len(vals), len(set(vals)), missing


def read_quota(fleet_dir):
    """The `quota` object of status.json, or None when the file does not carry one."""
    try:
        with open(os.path.join(fleet_dir, "status.json"), encoding="utf-8-sig") as f:
            q = (json.load(f) or {}).get("quota")
        return q if isinstance(q, dict) and q else None
    except Exception:
        return None


#: Fewer split trees than this is "direction only": a rate over a handful of trees is not a gate.
MIN_TREES = 20


def gate_summary(workers, campaign_lines, quota=None):
    """The pre-communication gate numbers as one dict (every figure derived, none invented).

    Keys: split_trees (roots that have at least one descendant), sample_ok (split_trees >=
    MIN_TREES), max_depth, max_descendants, max_subtree_turns, duplication (goal_hash / jid
    shared-row counts), merge (finished / non-done), quota (rpm / rph / refusal pressure, or
    None when status carries no quota object). A rate whose denominator is 0 is None.
    """
    tree = build_tree(workers, campaign_lines)
    shape = tree_shape(tree)
    split = [r for r in shape["per_root"] if r["descendants"] > 0]
    dup = {}
    for key in ("goal_hash", "jid"):
        n, distinct, missing = _duplication(workers, key)
        dup[key] = {"rows": n, "shared": n - distinct, "missing": missing}
    aggs = [n for n in tree["nodes"].values() if n["role"] == "aggregator" and n["state"] in _TERMINAL]
    q = None
    if quota:
        q = {k: quota.get(k) for k in ("rpm", "rph", "pct_rpm", "pct_rph", "refusals_5m")
             if isinstance(quota.get(k), (int, float)) and not isinstance(quota.get(k), bool)}
    return {"split_trees": len(split), "sample_ok": len(split) >= MIN_TREES,
            "max_depth": shape["max_depth"],
            "max_descendants": max([r["descendants"] for r in split] or [0]),
            "max_subtree_turns": max([r["subtree_turns"] for r in split] or [0]),
            "duplication": dup,
            "merge": {"finished": len(aggs), "not_done": sum(1 for n in aggs if n["state"] != "done")},
            "quota": q or None}


def render_gate(g):
    """Markdown for `gate_summary`; says plainly when the sample is too small to judge."""
    out = ["## Gate readout (before lateral communication)", ""]
    out.append("- split trees: %d (%s)" % (
        g["split_trees"],
        "enough to read" if g["sample_ok"]
        else "fewer than %d: direction only, NOT a judgement" % MIN_TREES))
    out.append("- deepest tree: %d  largest tree: %d descendants  most subtree turns: %d"
               % (g["max_depth"], g["max_descendants"], g["max_subtree_turns"]))
    for key, d in sorted(g["duplication"].items()):
        out.append("- duplication by %s: %s" % (key, _pct(d["shared"], d["rows"])))
    m = g["merge"]
    out.append("- merge failure: %s" % _pct(m["not_done"], m["finished"]))
    q = g["quota"]
    out.append("- quota pressure: " + (", ".join("%s=%s" % kv for kv in sorted(q.items()))
                                       if q else "unknown (no quota in status.json)"))
    out.append("")
    return out


def build_report(workers, campaign_lines, quota=None):
    tree = build_tree(workers, campaign_lines)
    shape = tree_shape(tree)
    nodes = tree["nodes"]
    out = ["# Task tree report", ""]
    out += render_gate(gate_summary(workers, campaign_lines, quota))
    out += ["## Size", "",
            "- rows read: %d%s" % (tree["total_input"],
                                   " (TRUNCATED, %d dropped)" % tree["dropped"] if tree["truncated"] else ""),
            "- nodes: %d  roots: %d  orphans: %d  nodes in/under a cycle: %d"
            % (shape["nodes"], shape["roots"], shape["orphans"], shape["cycle_nodes"]),
            "- unplaced rows (no identity beyond a worker name): %d" % shape["unlinked"],
            "- max depth (tree distance from top): %d" % shape["max_depth"],
            "- states: " + (", ".join("%s=%d" % kv for kv in sorted(shape["by_state"].items())) or "none"),
            ""]
    out += ["## Descendants and subtree turns per root (top %d)" % TOP_ROOTS, ""]
    if shape["per_root"]:
        out += ["| root | descendants | subtree turns |", "|---|---|---|"]
        out += ["| %s | %d | %d |" % (r["root"], r["descendants"], r["subtree_turns"])
                for r in shape["per_root"][:TOP_ROOTS]]
    else:
        out.append("none")
    out.append("")

    out += ["## Retry / duplication", ""]
    for key in ("goal_hash", "jid"):
        n, distinct, missing = _duplication(workers, key)
        out.append("- %s: %s rows share a value with another row (%d rows, %d distinct); %d rows lack it"
                   % (key, _pct(n - distinct, n), n, distinct, missing))
    out.append("")

    aggs = [n for n in nodes.values() if n["role"] == "aggregator" and n["state"] in _TERMINAL]
    bad = [n for n in aggs if n["state"] != "done"]
    out += ["## Merge", "",
            "- merge workers finished: %d; non-DONE: %s" % (len(aggs), _pct(len(bad), len(aggs))),
            ""]

    out += ["## Quota pressure", ""]
    if quota:
        out += ["- %s: %s" % (k, v) for k, v in sorted(quota.items())
                if isinstance(v, (int, float, str, bool))][:12] or ["- unavailable (no scalar fields)"]
    else:
        out.append("- unavailable (status.json carries no quota object)")
    out.append("")

    unknown_role = sum(1 for n in nodes.values() if n["role"] == "" and not n.get("virtual"))
    out += ["## Unknown", "",
            "- rows with no task_id, no campaign and no parent (placed by name only, not roots): %d"
            % tree["unlinked"],
            "- parents rebuilt from campaign headers (virtual nodes): %d" % shape["virtual"],
            "- rows with no task_id and no name (cannot be placed): %d" % tree["unidentified"],
            "- rows repeating an id already seen: %d" % tree["duplicate_ids"],
            "- nodes with no role: %d" % unknown_role,
            "- orphans (parent not in the data): %d%s"
            % (len(tree["orphans"]), " -> " + ", ".join(tree["orphans"][:10]) if tree["orphans"] else ""),
            "- cycles: %d" % len(tree["cycles"]),
            "- campaign child lines with no worker row: %d" % tree["ledger_only_children"],
            ""]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Markdown task-tree report from a .fleet directory.")
    ap.add_argument("--fleet-dir", default=os.path.join(REPO, ".fleet"),
                    help="directory holding status.json / campaigns.jsonl (read only)")
    ap.add_argument("--json", action="store_true", help="print the gate readout as JSON only")
    args = ap.parse_args(argv)
    workers, lines = read_fleet_dir(args.fleet_dir)
    quota = read_quota(args.fleet_dir)
    if args.json:
        print(json.dumps(gate_summary(workers, lines, quota), ensure_ascii=False, sort_keys=True))
        return 0
    print(build_report(workers, lines, quota))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
