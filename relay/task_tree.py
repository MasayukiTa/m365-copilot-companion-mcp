"""Task TREE derived read-only from the fleet's existing run state.

`relay.family_view` groups workers FLAT by campaign id. A worker row also carries `task_id`
and `parent_task_id`, so the same rows already describe a tree of arbitrary depth; this module
only reads that tree. Nothing is written, no field is added, and no existing module is touched.

Inputs (all existing):
  * the `workers` rows of `.fleet/status.json`: task_id, parent_task_id, campaign_id, role,
    depth, status, outcome, turn, jid, run_id, name;
  * the lines of `.fleet/campaigns.jsonl` (campaign header, child lines, marker lines).

`build_tree(workers, campaign_lines)` returns a plain dict:

    {
      "nodes":   {node_id: {
                    "id", "parent_id", "root_id", "depth", "declared_depth", "role", "state",
                    "merge_state", "turns", "children": [ids], "child_count",
                    "descendants", "subtree_turns", "campaign_id", "anomaly"}},
      "roots":   [ids],            # nodes with no parent_task_id at all
      "orphans": [ids],            # parent_task_id names a node that is not in the input
      "cycles":  [[ids]],          # parent chains that loop back on themselves
      "unidentified": 0,           # rows with neither task_id nor name (cannot be placed)
      "duplicate_ids": 0,          # rows that repeated an id already seen (last row wins)
      "ledger_only_children": 0,   # child lines in campaigns.jsonl with no worker row
      "truncated": False, "dropped": 0, "total_input": 0
    }

Rules the owner set for this view:
  * An orphan is REPORTED, never attached to a guessed parent. Its own subtree is still
    measured, with the orphan as the subtree's top.
  * A node in (or hanging under) a cycle has no root; `descendants` and `subtree_turns` are
    None there, meaning "not computed", never a made-up zero.
  * Missing or unknown keys (old rows) never raise. A field that cannot be read is "" / 0 and
    the node's identity falls back to the worker name (`w:<name>`) before the row is given up.
  * Bounded: at most MAX_NODES rows and MAX_LINES ledger lines are read.

Pure. The only I/O lives in `relay.family_view.read_fleet_dir`.
"""
from __future__ import annotations

from relay.family_view import _child_state
from relay.family_view import build_flat_groups as build_groups  # flat: no recursion into the tree

MAX_NODES = 20000
MAX_LINES = 200000

_NO_LEDGER = lambda goal, job_id="": ""  # noqa: E731  (never re-display a goal body)


#: An aggregator row is the merge step itself: its own state is the merge state.
_AGG_MERGE = {"queued": "merging", "running": "merging", "done": "merged",
              "failed": "failed", "interrupted": "failed"}


def _as_int(v):
    try:
        return max(int(v), 0)
    except Exception:
        return 0


def _node_id(w):
    tid = w.get("task_id")
    if tid not in (None, ""):
        return str(tid)
    name = str(w.get("name") or "")
    return "w:" + name if name else ""


def _parent_id(w):
    p = w.get("parent_task_id")
    return "" if p in (None, "") else str(p)


def _merge_states(ws, campaign_lines):
    """{campaign_id: merge_state} for every split group, reusing the flat grouping's rules."""
    try:
        return {g["group_id"]: g["merge_state"]
                for g in build_groups(ws, list(campaign_lines or [])[:MAX_LINES],
                                      ledger_fn=_NO_LEDGER)}
    except Exception:
        return {}


def _ledger_only_children(ids, campaign_lines):
    try:
        from relay.fanout import campaigns_from_ledger
        camps = campaigns_from_ledger(list(campaign_lines or [])[:MAX_LINES])
    except Exception:
        return 0
    seen = set(ids)
    n = 0
    for fam in camps.values():
        for rec in fam.get("children") or []:
            tid = rec.get("task_id") if isinstance(rec, dict) else None
            if tid not in (None, "") and str(tid) not in seen:
                n += 1
    return n


def _resolve(nodes):
    """Classify every node by walking its parent chain once, iteratively, with memo.

    Returns (top, kind): top[id] is the topmost reachable node id (the root, or the orphan that
    tops an orphaned subtree); kind[id] is "root" | "orphan" | "cycle" | "under_cycle"."""
    top, kind = {}, {}
    for start in nodes:
        if start in top or start in kind:
            continue
        path, pos, cur, verdict = [], {}, start, None
        while True:
            if cur in top or cur in kind:
                verdict = ("known", cur)
                break
            if cur in pos:
                verdict = ("cycle", pos[cur])
                break
            pos[cur] = len(path)
            path.append(cur)
            p = nodes[cur]["parent_id"]
            if p == "":
                verdict = ("root", None)
                break
            if p not in nodes:
                verdict = ("orphan", None)
                break
            cur = p
        tag, extra = verdict
        if tag == "cycle":
            for i, nid in enumerate(path):
                kind[nid] = "cycle" if i >= extra else "under_cycle"
        elif tag == "known":
            for nid in path:
                if extra in top:
                    top[nid] = top[extra]
                    kind[nid] = "member"
                else:
                    kind[nid] = "under_cycle"
        else:
            topid = path[-1]
            for nid in path:
                top[nid] = topid
                kind[nid] = "member"
            kind[topid] = tag
    return top, kind


def build_tree(workers, campaign_lines=None, max_nodes=MAX_NODES):
    """The task tree described in the module docstring. Never raises on bad rows."""
    rows = [w for w in (workers or []) if isinstance(w, dict)]
    out = {"nodes": {}, "roots": [], "orphans": [], "cycles": [], "unidentified": 0,
           "duplicate_ids": 0, "ledger_only_children": 0, "truncated": False, "dropped": 0,
           "total_input": len(rows)}
    if len(rows) > max_nodes:
        out["truncated"], out["dropped"] = True, len(rows) - max_nodes
        rows = rows[:max_nodes]

    merge = _merge_states(rows, campaign_lines)
    nodes = {}
    for w in rows:
        nid = _node_id(w)
        if not nid:
            out["unidentified"] += 1
            continue
        if nid in nodes:
            out["duplicate_ids"] += 1
        role = str(w.get("role") or "").lower()
        cid = str(w.get("campaign_id") or "")
        ms = ""
        if cid and role != "subtask":
            ms = (merge.get(cid, "") if role != "aggregator"
                  else _AGG_MERGE.get(_child_state(w), ""))
        nodes[nid] = {"id": nid, "parent_id": _parent_id(w), "root_id": "", "depth": 0,
                      "declared_depth": _as_int(w.get("depth")), "role": role,
                      "state": _child_state(w), "merge_state": ms,
                      "turns": _as_int(w.get("turn")), "children": [], "child_count": 0,
                      "descendants": None, "subtree_turns": None, "campaign_id": cid,
                      "anomaly": ""}

    top, kind = _resolve(nodes)
    for nid, n in nodes.items():
        k = kind.get(nid, "")
        if k in ("cycle", "under_cycle"):
            n["anomaly"] = k
        elif k == "orphan":
            n["anomaly"] = "orphan"
        p = n["parent_id"]
        if p in nodes and k not in ("cycle", "under_cycle"):
            nodes[p]["children"].append(nid)
        n["root_id"] = top.get(nid, "")
    for n in nodes.values():
        n["child_count"] = len(n["children"])

    # Depth, descendants and subtree turns: one pass from each top, iterative and cycle-free
    # (nodes in or under a cycle were never linked as children, so they are never reached).
    tops = [i for i, n in nodes.items() if n["anomaly"] in ("", "orphan") and n["parent_id"] not in nodes]
    for t in tops:
        order, stack = [], [t]
        nodes[t]["depth"] = 0
        while stack:
            cur = stack.pop()
            order.append(cur)
            for c in nodes[cur]["children"]:
                nodes[c]["depth"] = nodes[cur]["depth"] + 1
                stack.append(c)
        for cur in reversed(order):
            n = nodes[cur]
            n["descendants"] = sum(1 + nodes[c]["descendants"] for c in n["children"])
            n["subtree_turns"] = n["turns"] + sum(nodes[c]["subtree_turns"] for c in n["children"])

    out["nodes"] = nodes
    out["roots"] = [i for i, n in nodes.items()
                    if n["parent_id"] == "" and n["anomaly"] == ""]
    out["orphans"] = [i for i, n in nodes.items() if n["anomaly"] == "orphan"]
    out["cycles"] = _cycle_lists(nodes, kind)
    out["ledger_only_children"] = _ledger_only_children(nodes, campaign_lines)
    return out


def _cycle_lists(nodes, kind):
    seen, cycles = set(), []
    for nid in nodes:
        if kind.get(nid) != "cycle" or nid in seen:
            continue
        loop, cur = [], nid
        while cur not in seen and kind.get(cur) == "cycle":
            seen.add(cur)
            loop.append(cur)
            cur = nodes[cur]["parent_id"]
        cycles.append(sorted(loop))
    return sorted(cycles)


def tree_shape(tree):
    """Aggregate numbers over a `build_tree` result: sizes, depth, per-root descendants."""
    nodes = tree.get("nodes") or {}
    tops = list(tree.get("roots") or []) + list(tree.get("orphans") or [])
    per_root = sorted(((nodes[t]["descendants"], nodes[t]["subtree_turns"], t) for t in tops),
                      key=lambda r: (-r[0], r[2]))
    by_state = {}
    for n in nodes.values():
        by_state[n["state"]] = by_state.get(n["state"], 0) + 1
    return {"nodes": len(nodes), "roots": len(tree.get("roots") or []),
            "orphans": len(tree.get("orphans") or []),
            "cycle_nodes": sum(1 for n in nodes.values()
                               if n["anomaly"] in ("cycle", "under_cycle")),
            "max_depth": max([n["depth"] for n in nodes.values() if n["anomaly"] in ("", "orphan")] or [0]),
            "per_root": [{"root": r, "descendants": d, "subtree_turns": t} for d, t, r in per_root],
            "by_state": by_state}


__all__ = ["build_tree", "tree_shape", "MAX_NODES", "MAX_LINES"]
