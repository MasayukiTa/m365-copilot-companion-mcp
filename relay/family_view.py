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


def _merge_state(view_state, aggs, merged_flag):
    if any(_child_state(a) == "done" for a in aggs):
        return "merged"
    if any(_child_state(a) in ("running", "queued") for a in aggs):
        return "merging"
    if aggs:                       # all aggregators failed / interrupted
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


def build_groups(workers, campaign_lines=None, ledger_fn=None):
    """One summary dict (see module docstring) per split group. Never raises on bad rows."""
    ws = [w for w in (workers or []) if isinstance(w, dict)]
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
        merge = _merge_state(marker.get("fanin_state", "pending"), aggs, bool(camp.get("merged")))
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
    for g in groups or []:
        p, c = g["parent"], g["children"]
        lines.append("%s  親: %s" % (g["label"], p["display_label"] or p["status"] or "-"))
        lines.append("  子 %d 件: 待機 %d / 実行中 %d / 完了 %d / 失敗 %d / 中断 %d" % (
            g["children_total"], c["queued"], c["running"], c["done"], c["failed"],
            c["interrupted"]))
        lines.append("  統合: %s" % (g["merge_label"] or "-"))
        lg = g["ledger"]
        if lg["task"]:
            lines.append("  タスク: " + lg["task"])
        if lg["constraint_count"]:
            lines.append("  固定条件 %d 件: %s" % (lg["constraint_count"],
                                               ", ".join(lg["tokens"]) or "(語なし)"))
        lines.extend("  ! " + x for x in g["warnings"])
    return "\n".join(lines)


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


__all__ = ["build_groups", "annotate_display_state", "render_text", "read_fleet_dir",
           "parse_ledger", "DISPLAY_LABELS", "MERGE_LABELS"]
