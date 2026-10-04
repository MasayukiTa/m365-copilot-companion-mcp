"""Owner-facing ledger of a fan-out "split group" (分割グループ), derived read-only.

A long task is split into children plus a merge step. Today the owner sees N unrelated
rows, and the parent (real status done / outcome FANOUT, on purpose) looks FINISHED while
its children still run. This module turns the state the coordinator already writes into ONE
compact, ledger-style summary per group. Nothing is written, nothing is fabricated.

Inputs (all existing): the `workers` rows of `.fleet/status.json` (campaign_id, task_id,
parent_task_id, role, status, outcome, goal, name), and the lines of `.fleet/campaigns.jsonl`
(header with the parent goal, `{"kind":"merged"}` when the merge was queued).

Hard rules (owner):
  * The long goal text is NEVER re-displayed. Only the compact ledger that
    `relay_fleet.goal_ledger` already produces is used, and even its lines are clipped again
    here (TASK_CAP / CONSTRAINT_CAP), so the output is bounded whatever the goal is.
  * The parent's real `status` / `outcome` are untouched (FANOUT stays; relay_fleet.TERMINAL
    is untouched). `display_state` is a separate DERIVED field, see `annotate_display_state`.
  * "family" is an internal word; the user-visible wording is 分割グループ.

JSON contract (what a UI reads; `build_groups` returns a list of these, `render_text` a
plain-text form). Every key is always present; unknown/absent data is "" / 0 / [].

    {
      "group_id":        "cmp1",                 # campaign_id
      "label":           "分割グループ cmp1",
      "parent": {"name": "w0", "status": "done", "outcome": "FANOUT",   # REAL, unchanged
                 "display_state": "awaiting_children",                  # derived
                 "display_label": "待機中"},
      "children_total":  4,
      "children": {"queued": 0, "running": 2, "done": 1, "failed": 0, "interrupted": 1},
      "merge_state":     "pending",   # pending|ready|merging|merged|failed
      "merge_label":     "子の完了待ち",
      "ledger": {"task": "<first sentence, <=160 chars>",
                 "constraints": ["<ledger line, <=120 chars>", ...],   # <=5
                 "constraint_count": 2,          # lines the ledger carries (before the cap)
                 "tokens": ["2026-10-01", "\\u300cfoo\\u300d", "500\\u5186"]},  # <=8, from those lines
      "warnings": []                             # e.g. "interrupted child"
    }

NESTING (additive; every key above is unchanged). A child slot that fans out again hangs a
nested group under it; the link comes from the recorded tree identity (campaign header
parent_task_id / parent_campaign_id / root_id and the worker rows' parent_task_id, read through
relay.task_tree). Each group also carries:

      "parent_group_id":  None,   # campaign_id of the enclosing group; None for a root group
      "root_id":          "cmp1", # the topmost group (a recorded root_id for an orphan)
      "depth":            0,      # 0 = root group (a flat fleet: every group is 0)
      "child_group_ids":  [],     # nested groups, sorted, clipped at MAX_CHILD_IDS (then
                                  #   "child_group_ids_truncated": <n clipped> is added)
      "descendant_count": 0,      # nested groups below this one (exact, not clipped)
      "descendant_turns": 0,      # sum of worker `turn` over those nested groups
      "orphan":           False   # a parent was recorded but is not in the data (reported, never
                                  #   attached to a guess); loops / chains deeper than
                                  #   MAX_DEPTH are cut the same way

`merge_state` additionally takes "waiting_on_subgroups" (a nested group under one of its slots is
not merged yet) and "unknown" (a slot reports FANOUT but no nested group is found for it, or a
nested group is itself unknown): derived, never guessed. Both only replace pending/ready.

display_state (parent rows only; children/solo rows get none): awaiting_children (children
still queued/running: shown 待機中 so it does not look finished), ready_to_merge (all children
done, merge not started), merging, done (merged), merge_failed, interrupted (an unfinished
child was interrupted by a dead coordinator). Anything else is not a fan-out wait and the
worker's own status is passed through.

Pure. The only I/O is `read_fleet_dir`, which tolerates missing/torn files.
"""
from __future__ import annotations

import json
import os
import re

from relay.fanout import campaigns_from_ledger, fanout_family_view

TASK_CAP = 160
CONSTRAINT_CAP = 120
MAX_CONSTRAINTS = 5
MAX_TOKENS = 8
TOKEN_CAP = 30
MAX_DEPTH = 16            # deeper chains are cut: the over-deep group is reported as an orphan
MAX_CHILD_IDS = 50        # child_group_ids is clipped here; descendant_count stays exact
MAX_TREE_ROWS = 20000     # rows handed to relay.task_tree

_FAILED = {"stuck", "maxturns", "error", "cancelled", "content_refused"}
_QUEUED = {"", "pending", "queued"}

DISPLAY_LABELS = {
    "awaiting_children": "待機中",
    "ready_to_merge": "統合待ち",
    "merging": "統合中",
    "done": "完了",
    "merge_failed": "統合失敗",
    "interrupted": "中断",
}
MERGE_LABELS = {
    "pending": "子の完了待ち",
    "ready": "統合待ち",
    "merging": "統合中",
    "merged": "統合済み",
    "failed": "統合失敗",
    "waiting_on_subgroups": "下位グループの統合待ち",
    "unknown": "不明",
}

# Tokens a ledger line can carry: quoted names, ISO/slash dates, times, amounts/counts.
_TOKEN_RE = re.compile(
    r"「[^」]{1,24}」|\"[^\"]{1,24}\"|\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?|\d{1,2}:\d{2}"
    r"|\d[\d,\.]*\s?(?:円|万円|件|人|%|％|KB|MB|GB|時間|分|日|個|行|列|枚)")


def _clip(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:max(n - 1, 0)] + "…"


def _child_state(w):
    status = str(w.get("status") or "").lower()
    outcome = str(w.get("outcome") or "").upper()
    if status == "interrupted" or outcome == "INTERRUPTED":
        return "interrupted"
    if status in _FAILED:
        return "failed"
    if status == "done":
        return "done" if outcome in ("", "DONE", "FANOUT") else "failed"
    if status in _QUEUED and not outcome:
        return "queued"
    return "running"


def parse_ledger(ledger):
    """Split a goal_ledger() string into (task, constraint_lines, tokens). Clipped, bounded."""
    task, cons, in_cons = "", [], False
    for raw in str(ledger or "").splitlines():
        line = raw.strip()
        if line.startswith("タスク:"):
            task, in_cons = line[len("タスク:"):].strip(), False
        elif line.startswith("固定条件:"):
            in_cons = True
        elif line.startswith("- ") and in_cons:
            cons.append(line[2:].strip())
        else:
            in_cons = False
    tokens = []
    for c in cons:
        for t in _TOKEN_RE.findall(c):
            t = _clip(t, TOKEN_CAP)
            if t not in tokens:
                tokens.append(t)
    return (_clip(task, TASK_CAP), [_clip(c, CONSTRAINT_CAP) for c in cons[:MAX_CONSTRAINTS]],
            len(cons), tokens[:MAX_TOKENS])


def _default_ledger_fn():
    try:
        from relay.relay_fleet import goal_ledger
        return goal_ledger
    except Exception:
        return lambda goal, job_id="": ""


def _merge_state(view_state, aggs, merged_flag, abandoned=False):
    if any(_child_state(a) == "done" for a in aggs):
        return "merged"
    if any(_child_state(a) in ("running", "queued") for a in aggs):
        return "merging"
    if aggs:                       # all aggregators failed / interrupted
        return "failed"
    if abandoned:                  # ledger: the merge was lost twice and written off
        return "failed"
    if merged_flag:
        return "merging"           # ledger says the merge was queued, worker not visible yet
    return view_state if view_state in ("pending", "ready") else "pending"


def _display_state(parent, counts, merge):
    if str(parent.get("outcome") or "").upper() != "FANOUT":
        return ""                  # not a fan-out wait: the worker's own status speaks
    if merge == "merged":
        return "done"
    if merge == "failed":
        return "merge_failed"
    if merge == "merging":
        return "merging"
    if counts["interrupted"]:
        return "interrupted"
    if merge == "ready":
        return "ready_to_merge"
    return "awaiting_children"


def build_flat_groups(workers, campaign_lines=None, ledger_fn=None):
    """One summary dict per split group, grouped FLAT by campaign id (no tree keys).
    Never raises on bad rows. `build_groups` adds the nesting on top of this."""
    ws =[w for w in (workers or []) if isinstance(w, dict)]
    try:
        camps = campaigns_from_ledger(campaign_lines or [])
    except Exception:
        camps = {}
    try:
        view = fanout_family_view(ws)
    except Exception:
        view = {}
    ledger_fn = ledger_fn or _default_ledger_fn()
    by_cid = {}
    for w in ws:
        cid = str(w.get("campaign_id") or "")
        if cid:
            by_cid.setdefault(cid, []).append(w)
    out = []
    for cid, members in by_cid.items():
        kids = [w for w in members if str(w.get("role") or "").lower() == "subtask"]
        aggs = [w for w in members if str(w.get("role") or "").lower() == "aggregator"]
        parents = [w for w in members if w not in kids and w not in aggs]
        if not kids and not aggs:
            continue               # a solo worker, or a proposed split that never ran
        parent = parents[0] if parents else {}
        counts = {"queued": 0, "running": 0, "done": 0, "failed": 0, "interrupted": 0}
        for k in kids:
            counts[_child_state(k)] += 1
        pname = str(parent.get("name") or "")
        marker = view.get(pname) or view.get(str((aggs or kids)[0].get("name") or "")) or {}
        camp = camps.get(cid) or {}
        merge = _merge_state(marker.get("fanin_state", "pending"), aggs, bool(camp.get("merged")),
                       abandoned=bool(camp.get("merge_abandoned")))
        goal = camp.get("goal") or parent.get("goal") or ""
        try:
            ledger = ledger_fn(goal, str(parent.get("task_id") or "")) or ""
        except Exception:
            ledger = ""
        task, cons, n_cons, tokens = parse_ledger(ledger)
        ds = _display_state(parent, counts, merge) if parent else ""
        warns = []
        if counts["interrupted"]:
            warns.append("中断された子が %d 件あります" % counts["interrupted"])
        if counts["failed"]:
            warns.append("失敗した子が %d 件あります" % counts["failed"])
        out.append({
            "group_id": cid,
            "label": "分割グループ " + cid,
            "parent": {"name": pname,
                       "status": str(parent.get("status") or ""),
                       "outcome": str(parent.get("outcome") or ""),
                       "display_state": ds,
                       "display_label": DISPLAY_LABELS.get(ds, "")},
            "children_total": len(kids),
            "children": counts,
            "merge_state": merge,
            "merge_label": MERGE_LABELS.get(merge, ""),
            "ledger": {"task": task, "constraints": cons, "constraint_count": n_cons,
                       "tokens": tokens},
            "warnings": warns,
        })
    return sorted(out, key=lambda g: g["group_id"])


def _as_int(v):
    try:
        return max(int(v), 0)
    except Exception:
        return 0


def _headers(campaign_lines):
    """{campaign_id: {parent_task_id, parent_campaign_id, root_id}} from campaign header lines."""
    out = {}
    for line in campaign_lines or []:
        line = (line or "").strip() if isinstance(line, str) else ""
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict) and rec.get("kind") == "campaign" and rec.get("campaign_id"):
            out[str(rec["campaign_id"])] = {
                "parent_task_id": str(rec.get("parent_task_id") or ""),
                "parent_campaign_id": str(rec.get("parent_campaign_id") or ""),
                "root_id": str(rec.get("root_id") or "")}
    return out


def _link_parents(groups, ws, heads):
    """{cid: (parent_cid or None, slot_task_id or "", orphan)} using the recorded tree identity.

    A group's splitting parent P is the header's parent_task_id (or the common parent_task_id of
    its children). If P is a row of another campaign, P is the slot it hangs from there. If P is
    the group's own parent row, that row's own parent_task_id names the slot. A recorded parent
    that cannot be found is an ORPHAN: reported, never attached to a guess."""
    try:
        from relay.task_tree import build_tree
        nodes = build_tree(ws, [], max_nodes=MAX_TREE_ROWS).get("nodes") or {}
    except Exception:
        nodes = {}
    by_cid = {}
    for w in ws:
        cid = str(w.get("campaign_id") or "")
        if cid:
            by_cid.setdefault(cid, []).append(w)
    links = {}
    for cid in groups:
        h = heads.get(cid) or {}
        mem = by_cid.get(cid, [])
        kid_p = {str(w.get("parent_task_id")) for w in mem
                 if str(w.get("role") or "").lower() in ("subtask", "aggregator")
                 and w.get("parent_task_id") not in (None, "")}
        ptid = h.get("parent_task_id") or (next(iter(kid_p)) if len(kid_p) == 1 else "")
        pcid = h.get("parent_campaign_id") or ""
        P = nodes.get(ptid) if ptid else None
        pc, slot, recorded = None, "", bool(pcid)
        if P and P.get("anomaly") not in ("cycle", "under_cycle"):
            if P["campaign_id"] and P["campaign_id"] != cid:
                pc, slot = P["campaign_id"], P["id"]
            elif P["parent_id"]:
                recorded = True
                Q = nodes.get(P["parent_id"])
                if Q and Q["campaign_id"] and Q["campaign_id"] != cid:
                    pc, slot = Q["campaign_id"], Q["id"]
        if pc is None and pcid in groups and pcid != cid:
            pc = pcid
        if pc is not None and pc not in groups:
            pc, slot = None, ""
        links[cid] = (pc, slot, pc is None and recorded)
    # Cut loops and over-deep chains: those groups are reported as orphans.
    for cid in list(links):
        seen, cur = [], cid
        while cur is not None and cur not in seen and len(seen) <= MAX_DEPTH:
            seen.append(cur)
            cur = links[cur][0]
        if cur is not None:
            links[cid] = (None, "", True)
    return links


def build_groups(workers, campaign_lines=None, ledger_fn=None):
    """`build_flat_groups` plus the nesting keys (see module docstring). A flat fleet keeps every
    existing key byte-identical and gets neutral values for the new ones."""
    ws = [w for w in (workers or []) if isinstance(w, dict)]
    flat = build_flat_groups(ws, campaign_lines, ledger_fn)
    gmap = {g["group_id"]: g for g in flat}
    try:
        links = _link_parents(gmap, ws, _headers(campaign_lines))
    except Exception:
        links = {cid: (None, "", False) for cid in gmap}
    children = {cid: [] for cid in gmap}
    for cid, (pc, _slot, _o) in links.items():
        if pc is not None:
            children[pc].append(cid)
    turns, fan_slots = {}, {}
    for w in ws:
        cid = str(w.get("campaign_id") or "")
        if cid in gmap:
            turns[cid] = turns.get(cid, 0) + _as_int(w.get("turn"))
            if (str(w.get("role") or "").lower() == "subtask"
                    and str(w.get("outcome") or "").upper() == "FANOUT"):
                fan_slots.setdefault(cid, []).append(str(w.get("task_id") or ""))
    depth, root = {}, {}
    for cid in sorted(gmap):
        chain, cur = [], cid
        while cur is not None:
            chain.append(cur)
            cur = links[cur][0]
        top = chain[-1]
        recorded_root = ""
        if links[top][2]:          # orphan: keep the root the data itself recorded, if any
            recorded_root = next((str(w.get("root_id")) for w in ws
                                  if str(w.get("campaign_id") or "") == top and w.get("root_id")), "")
        for i, c in enumerate(reversed(chain)):
            depth[c], root[c] = i, recorded_root or top
    # Bottom-up so a parent sees its children's FINAL merge state.
    desc, dturns = {}, {}
    for cid in sorted(gmap, key=lambda c: (-depth[c], c)):
        desc[cid] = sum(1 + desc[c] for c in children[cid])
        dturns[cid] = sum(turns.get(c, 0) + dturns[c] for c in children[cid])
        g = gmap[cid]
        waiting = unknown = False
        for c in children[cid]:
            cm = gmap[c]["merge_state"]
            # a failed (abandoned) subgroup is a MISSING slot for the parent's merge, not a wait
            waiting = waiting or cm not in ("merged", "failed")
            unknown = unknown or cm == "unknown"
            if cm == "failed":
                g["warnings"].append("統合に失敗したサブグループがあります: " + c)
        slots = {links[c][1] for c in children[cid]}
        # a fan-out slot with no group under it: not known, not guessed
        if any(t not in slots for t in fan_slots.get(cid, ())):
            unknown = True
        if g["merge_state"] in ("pending", "ready") and (waiting or unknown):
            g["merge_state"] = "unknown" if unknown else "waiting_on_subgroups"
            g["merge_label"] = MERGE_LABELS[g["merge_state"]]
            if g["parent"]["display_state"] in ("ready_to_merge", "awaiting_children"):
                g["parent"]["display_state"] = "awaiting_children"
                g["parent"]["display_label"] = DISPLAY_LABELS["awaiting_children"]
    for cid, g in gmap.items():
        kids = sorted(children[cid])
        g.update({"parent_group_id": links[cid][0], "root_id": root[cid], "depth": depth[cid],
                  "child_group_ids": kids[:MAX_CHILD_IDS], "descendant_count": desc[cid],
                  "descendant_turns": dturns[cid], "orphan": bool(links[cid][2])})
        if len(kids) > MAX_CHILD_IDS:
            g["child_group_ids_truncated"] = len(kids) - MAX_CHILD_IDS
    return flat


def annotate_display_state(workers, campaign_lines=None):
    """Copies of `workers` where each fan-out PARENT gains `display_state` (derived only).
    status / outcome are never modified; other rows are returned unchanged."""
    groups = {g["parent"]["name"]: g["parent"]["display_state"]
              for g in build_groups(workers, campaign_lines, ledger_fn=lambda g, j="": "")
              if g["parent"]["name"] and g["parent"]["display_state"]}
    res = []
    for w in workers or []:
        if isinstance(w, dict) and w.get("name") in groups:
            w = dict(w, display_state=groups[w["name"]])
        res.append(w)
    return res


def render_text(groups):
    """Plain, ledger-style text (no goal body): one block per group."""
    lines = []
    for g in _tree_order(groups or []):
        p, c = g["parent"], g["children"]
        pad = "    " * min(int(g.get("depth") or 0), MAX_DEPTH)
        block = ["%s  親: %s" % (g["label"], p["display_label"] or p["status"] or "-"),
                 "  子 %d 件: 待機 %d / 実行中 %d / 完了 %d / 失敗 %d / 中断 %d" % (
                     g["children_total"], c["queued"], c["running"], c["done"], c["failed"],
                     c["interrupted"]),
                 "  統合: %s" % (g["merge_label"] or "-")]
        if g.get("parent_group_id"):
            block.append("  親グループ: %s" % g["parent_group_id"])
        if g.get("descendant_count"):
            block.append("  下位グループ %d 件 (計 %d ターン)" % (
                g["descendant_count"], g.get("descendant_turns") or 0))
        lg = g["ledger"]
        if lg["task"]:
            block.append("  タスク: " + lg["task"])
        if lg["constraint_count"]:
            block.append("  固定条件 %d 件: %s" % (lg["constraint_count"],
                                               ", ".join(lg["tokens"]) or "(語なし)"))
        block.extend("  ! " + x for x in g["warnings"])
        lines.extend(pad + b for b in block)
    return "\n".join(lines)


def _tree_order(groups):
    """Groups in depth-first tree order (children under their parent); a flat list is unchanged.
    Groups whose parent is not in the list are treated as roots."""
    ids = {g["group_id"] for g in groups}
    kids = {}
    for g in groups:
        pid = g.get("parent_group_id")
        if pid and pid in ids and pid != g["group_id"]:
            kids.setdefault(pid, []).append(g)
    out, seen = [], set()
    stack = [g for g in reversed(groups)
             if not (g.get("parent_group_id") in ids and g.get("parent_group_id") != g["group_id"])]
    while stack:
        g = stack.pop()
        if g["group_id"] in seen:
            continue
        seen.add(g["group_id"])
        out.append(g)
        stack.extend(reversed(kids.get(g["group_id"], [])))
    out.extend(g for g in groups if g["group_id"] not in seen)   # anything a loop hid
    return out


def read_fleet_dir(fleet_dir):
    """(workers, campaign_lines) from `.fleet/`; ([], []) for anything missing or torn."""
    workers, lines = [], []
    try:
        with open(os.path.join(fleet_dir, "status.json"), encoding="utf-8-sig") as f:
            workers = (json.load(f) or {}).get("workers") or []
    except Exception:
        workers = []
    try:
        with open(os.path.join(fleet_dir, "campaigns.jsonl"), encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except Exception:
        lines = []
    return workers, lines


def main(argv=None):
    """`python -m relay.family_view [--fleet-dir DIR]`: print the split-group ledger as text."""
    import argparse
    ap = argparse.ArgumentParser(description="Show the split-group (分割グループ) ledger.")
    ap.add_argument("--fleet-dir", default=".fleet", help="directory holding status.json")
    args = ap.parse_args(argv)
    workers, lines = read_fleet_dir(args.fleet_dir)
    print(render_text(build_groups(workers, lines)))
    return 0


__all__ = ["build_groups", "build_flat_groups","annotate_display_state", "render_text", "read_fleet_dir",
           "parse_ledger", "DISPLAY_LABELS", "MERGE_LABELS"]


if __name__ == "__main__":
    raise SystemExit(main())
