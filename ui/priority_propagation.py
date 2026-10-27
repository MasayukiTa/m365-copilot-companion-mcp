# -*- coding: utf-8 -*-
"""Propagate a manual "make this top priority" boost from a parent task down to its
split/campaign children -- as a PURE FUNCTION over the fleet's existing run state.

WHY THIS EXISTS. The cockpit lets an operator mark a waiting/ready task as top priority
when an urgent request is stuck behind a crowd of other work. A long goal, though, is
often not one task: `relay.fanout` splits it into independent children that each run in
their own conversation (task_id / parent_task_id / campaign_id / depth carried on every
worker row). Boosting only the parent row would leave the real work -- the children --
queued behind everyone else. The owner asked that boosting a parent reach its children
(requirement 4). This module is the decision half of that: given the directly-boosted
task ids and the worker rows, it returns the FULL set of task ids that should be treated
as boosted, the parent's own id plus every descendant in its split/campaign subtree.

WHAT A PARENT-CHILD LINK IS HERE (confirmed against relay.task_tree / relay.family_view):
  * every worker row carries `task_id` and `parent_task_id`; a child's `parent_task_id`
    names its parent's `task_id`. That is the same tree `relay.task_tree.build_tree`
    reads, of arbitrary depth.
  * a fan-out parent's own row may carry no `task_id` (a producer). Its identity then
    falls back to its `campaign_id`, and its children's `parent_task_id` names that
    campaign id. We mirror `relay.task_tree._node_id` so the same rows resolve the same
    way, and we also treat `campaign_id` / `root_id` as a second linkage so an archived
    middle parent never hides the children of a boosted root.

WHAT THIS MODULE DOES NOT DO. It does not read or write any file, does not change any
worker row, and does not decide WHO may boost or HOW a boost is recorded (that is the
cockpit's and the ledger's job). It only answers: given these boosted ids and these
rows, which ids are boosted once the boost flows downhill. Pure, bounded, and safe on
malformed rows -- a bad row is skipped, never raised on.

The flag field name is a parameter (`flag_field`, default "priority_boost") so this
composes with whatever the cockpit/ledger settle on for the on-row flag, without this
module having to know it. `boosted_ids_from_rows` reads that flag off the rows;
`effective_boosted_ids` takes an explicit id set so it can be driven from a ledger too.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

#: Default name of the on-row boolean that marks a directly-boosted task.
DEFAULT_FLAG_FIELD = "priority_boost"

#: Bound: never walk more than this many rows. The cockpit never shows more, and an
#: unbounded walk on a corrupt status.json is exactly the stall this feature fights.
MAX_ROWS = 20000

#: Bound on a single parent-chain walk, so a `parent_task_id` cycle cannot hang.
_MAX_CHAIN = 64


def _s(v: Any) -> str:
    """A row value as a trimmed string; None/missing become ""."""
    if v in (None, ""):
        return ""
    try:
        return str(v).strip()
    except Exception:
        return ""


def _node_id(row: Dict[str, Any]) -> str:
    """The id a row is known by, mirroring relay.task_tree._node_id.

    A row's own `task_id` wins. A root parent (a producer with no task_id whose role is
    not a subtask/aggregator) is known by its `campaign_id`. Otherwise it falls back to
    `w:<name>` so a nameable row is still placeable.
    """
    tid = _s(row.get("task_id"))
    if tid:
        return tid
    cid = _s(row.get("campaign_id"))
    role = _s(row.get("role")).lower()
    if cid and role not in ("subtask", "aggregator"):
        return cid
    name = _s(row.get("name"))
    return ("w:" + name) if name else ""


def _parent_id(row: Dict[str, Any]) -> str:
    return _s(row.get("parent_task_id"))


def _clean_rows(workers: Optional[Iterable[Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for w in (workers or []):
        if isinstance(w, dict):
            rows.append(w)
            if len(rows) >= MAX_ROWS:
                break
    return rows


def _child_index(rows: List[Dict[str, Any]]) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    """Build (parent_id -> [child node ids], node_id -> campaign_id).

    A child is linked to its parent two ways, and both are indexed so a boost reaches a
    child even when a middle parent row was archived out of status.json:
      * directly, parent_task_id -> the node whose id it names;
      * by campaign, so a boosted root (known by its campaign id) also reaches every row
        that carries that campaign_id, which is how fan-out children record their family.
    """
    by_parent: Dict[str, List[str]] = {}
    by_campaign: Dict[str, List[str]] = {}
    node_campaign: Dict[str, str] = {}
    for w in rows:
        nid = _node_id(w)
        if not nid:
            continue
        cid = _s(w.get("campaign_id"))
        node_campaign[nid] = cid
        pid = _parent_id(w)
        if pid and pid != nid:
            by_parent.setdefault(pid, []).append(nid)
        if cid:
            by_campaign.setdefault(cid, []).append(nid)
    # Fold the campaign linkage into the parent map: a node known by its campaign id is
    # the top of that campaign, so every row carrying the campaign id hangs under it.
    for cid, members in by_campaign.items():
        if cid in node_campaign:  # the campaign id is itself a node (a root parent row)
            for m in members:
                if m != cid:
                    by_parent.setdefault(cid, []).append(m)
    # De-duplicate child lists while keeping them stable.
    for pid, kids in by_parent.items():
        seen: Set[str] = set()
        uniq: List[str] = []
        for k in kids:
            if k not in seen:
                seen.add(k)
                uniq.append(k)
        by_parent[pid] = uniq
    return by_parent, node_campaign


def boosted_ids_from_rows(workers: Optional[Iterable[Any]],
                          flag_field: str = DEFAULT_FLAG_FIELD) -> Set[str]:
    """The set of DIRECTLY-boosted node ids, read off `flag_field` on each row.

    A row is directly boosted when its `flag_field` is truthy. The returned ids are node
    ids (via `_node_id`), so they line up with `effective_boosted_ids`.
    """
    out: Set[str] = set()
    for w in _clean_rows(workers):
        if bool(w.get(flag_field)):
            nid = _node_id(w)
            if nid:
                out.add(nid)
    return out


def effective_boosted_ids(boosted_ids: Iterable[str],
                          workers: Optional[Iterable[Any]]) -> Set[str]:
    """Every node id that is boosted once a parent's boost flows down to its children.

    Pure. Given the directly-boosted ids and the worker rows, returns the closure of
    those ids under the parent -> child relation (task_id/parent_task_id, plus the
    campaign linkage). The input ids are always included, even if no row matches them
    (a boosted parent whose row was archived still boosts its recorded children). A
    `parent_task_id` cycle cannot make this loop: each node is expanded at most once.
    """
    rows = _clean_rows(workers)
    by_parent, _ = _child_index(rows)
    seed = {_s(b) for b in (boosted_ids or []) if _s(b)}
    effective: Set[str] = set(seed)
    stack: List[str] = list(seed)
    steps = 0
    while stack:
        steps += 1
        if steps > MAX_ROWS * 4:  # defensive: the child map is a DAG after de-dup, but
            break                 # a corrupt map must never spin forever
        cur = stack.pop()
        for kid in by_parent.get(cur, ()):
            if kid not in effective:
                effective.add(kid)
                stack.append(kid)
    return effective


def propagate_boost(workers: Optional[Iterable[Any]],
                    flag_field: str = DEFAULT_FLAG_FIELD,
                    extra_boosted_ids: Optional[Iterable[str]] = None) -> Set[str]:
    """Convenience: read the directly-boosted ids off the rows (via `flag_field`), add
    any `extra_boosted_ids` (e.g. ids a ledger recorded for rows no longer present), and
    return the effective boosted set. This is the one call the cockpit tick needs.
    """
    direct = boosted_ids_from_rows(workers, flag_field=flag_field)
    if extra_boosted_ids:
        direct |= {_s(b) for b in extra_boosted_ids if _s(b)}
    return effective_boosted_ids(direct, workers)


def is_boosted(node_id: str, effective: Set[str]) -> bool:
    """True when `node_id` is in the effective boosted set. A tiny helper so callers do
    not reach into the set with their own string-coercion rules."""
    return _s(node_id) in (effective or set())


__all__ = [
    "DEFAULT_FLAG_FIELD",
    "MAX_ROWS",
    "boosted_ids_from_rows",
    "effective_boosted_ids",
    "propagate_boost",
    "is_boosted",
]
