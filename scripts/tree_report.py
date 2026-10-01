"""Read-only markdown report on the task tree held in a `.fleet` directory.

    python scripts/tree_report.py [--fleet-dir DIR]

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


def build_report(workers, campaign_lines, quota=None):
    tree = build_tree(workers, campaign_lines)
    shape = tree_shape(tree)
    nodes = tree["nodes"]
    out = ["# Task tree report", ""]
    out += ["## Size", "",
            "- rows read: %d%s" % (tree["total_input"],
                                   " (TRUNCATED, %d dropped)" % tree["dropped"] if tree["truncated"] else ""),
            "- nodes: %d  roots: %d  orphans: %d  nodes in/under a cycle: %d"
            % (shape["nodes"], shape["roots"], shape["orphans"], shape["cycle_nodes"]),
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

    unknown_role = sum(1 for n in nodes.values() if n["role"] == "")
    out += ["## Unknown", "",
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
    args = ap.parse_args(argv)
    workers, lines = read_fleet_dir(args.fleet_dir)
    print(build_report(workers, lines, read_quota(args.fleet_dir)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
