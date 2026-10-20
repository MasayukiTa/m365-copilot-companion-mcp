# -*- coding: utf-8 -*-
"""Agent roster: who is working on what, DERIVED read-only from the workers of status.json.

Nothing is stored. The coordinator already writes `workers` (name, status, turn, campaign_id,
role, goal_summary) into status.json on every snapshot; this module only regroups those rows per
campaign. Two readers, neither of which keeps anything:

  * the coordinator adds `roster_block(workers)` to the snapshot it writes anyway
    (relay/fleet_runner.py), and the cockpit (ui/FleetCockpit.Roster.cs) paints it;
  * the board digest tool (tools/board_tool.py) passes `lines_for_campaign(...)` into
    relay.agent_board.digest when the board mode is p0 or p1.

Bounds (whatever the input is, the output is bounded): at most MAX_CAMPAIGNS campaigns, at most
MAX_WORKERS_PER_CAMPAIGN workers each, every text clipped. The task text is `goal_summary` only
(the compact label status.json already carries); the full goal is never read.

JSON contract (every key always present):

    {"total": 7,                    # worker rows seen
     "shown": 6,                    # rows listed below
     "omitted": 1,                  # total - shown
     "campaigns": [{"campaign_id": "cmp1",       # "" = workers that belong to no campaign
                    "count": 3,                  # rows in the campaign, before the clip
                    "workers": [{"name": "w1", "role": "child", "status": "waiting",
                                 "turn": 2, "task": "<goal_summary, <=TASK_CHARS>"}]}]}
"""
from __future__ import annotations

import re

MAX_CAMPAIGNS = 8
MAX_WORKERS_PER_CAMPAIGN = 8
NAME_CHARS = 24
ROLE_CHARS = 16
STATUS_CHARS = 16
TASK_CHARS = 60
CAMPAIGN_CHARS = 40
LINE_CHARS = 100
UNKNOWN = "unknown"

# Worker statuses that mean "not working any more"; they sort after the live ones. Any other
# status (including one this module has never heard of) is shown as written, clipped.
_FINISHED = frozenset({"done", "stuck", "maxturns", "error", "cancelled", "content_refused",
                       "interrupted"})
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


def _clip(value, n):
    """A one-line string of at most `n` characters. Non-strings and empty values give ""."""
    if not isinstance(value, str):
        return ""
    s = " ".join(_CTRL.sub(" ", value).split())
    return s if len(s) <= n else s[:max(0, n - 1)] + "…"


def _turn(value):
    try:
        if isinstance(value, bool):
            return 0
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _row(w):
    status = _clip(w.get("status"), STATUS_CHARS) or UNKNOWN
    return {"name": _clip(w.get("name"), NAME_CHARS) or "?",
            "role": _clip(w.get("role"), ROLE_CHARS),
            "status": status,
            "turn": _turn(w.get("turn")),
            "task": _clip(w.get("goal_summary"), TASK_CHARS)}


def build_roster(workers):
    """The roster of `workers` (the list from status.json). Never raises; junk rows are skipped
    (and not counted), a non-list gives the empty roster."""
    empty = {"total": 0, "shown": 0, "omitted": 0, "campaigns": []}
    try:
        if not isinstance(workers, (list, tuple)):
            return empty
        groups, order = {}, []
        total = 0
        for w in workers:
            if not isinstance(w, dict):
                continue
            total += 1
            cid = _clip(w.get("campaign_id"), CAMPAIGN_CHARS)
            if cid not in groups:
                groups[cid] = []
                order.append(cid)
            groups[cid].append(_row(w))
        # campaigns first (in first-seen order), the campaign-less workers last
        order.sort(key=lambda c: c == "")
        campaigns, shown = [], 0
        for cid in order[:MAX_CAMPAIGNS]:
            rows = groups[cid]
            rows = sorted(rows, key=lambda r: r["status"] in _FINISHED)   # stable: live first
            kept = rows[:MAX_WORKERS_PER_CAMPAIGN]
            shown += len(kept)
            campaigns.append({"campaign_id": cid, "count": len(rows), "workers": kept})
        return {"total": total, "shown": shown, "omitted": total - shown, "campaigns": campaigns}
    except Exception:
        return empty


def roster_block(workers):
    """`{"roster": ...}` to merge into the status snapshot, or {} when nobody is listed."""
    r = build_roster(workers)
    return {"roster": r} if r["total"] else {}


def _line(row):
    s = "%s [%s/%s] t%d" % (row["name"], row["role"] or "-", row["status"], row["turn"])
    if row["task"]:
        s += " " + row["task"]
    return s if len(s) <= LINE_CHARS else s[:LINE_CHARS - 1] + "…"


def render_lines(roster, campaign_id=None, max_lines=8):
    """Plain text lines of a roster (one per worker), at most `max_lines`, each at most
    LINE_CHARS. With `campaign_id`, only that campaign's workers."""
    try:
        if not isinstance(roster, dict):
            return []
        out = []
        for c in roster.get("campaigns") or []:
            if campaign_id is not None and c.get("campaign_id") != campaign_id:
                continue
            for r in c.get("workers") or []:
                out.append(" " + _line(r))
        return out[:max(0, int(max_lines))]
    except Exception:
        return []


def lines_for_campaign(workers, campaign_id, max_lines=6):
    """The roster lines of one campaign from raw status workers; [] when there are none."""
    if not isinstance(campaign_id, str) or not campaign_id:
        return []
    return render_lines(build_roster(workers), _clip(campaign_id, CAMPAIGN_CHARS), max_lines)
