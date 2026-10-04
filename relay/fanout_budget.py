# -*- coding: utf-8 -*-
"""A per-tree budget for fan-out: how many workers, how much work, how long one root may use.

WHY. A fan-out tree is a root campaign and everything split from it (every worker carries the
tree's `root_id`). Today the tree is bounded only by two accidents: a child cannot split again
(MAX_DEPTH = 1) and one split yields at most MAX_CHILDREN. Both are properties of the current
shape, not limits on the tree, so the day depth grows nothing here would stop one goal from
multiplying workers. This module is the limit that does not depend on the shape.

WHAT IT IS. Two pure functions and a few helpers around them:

  usage_from_status(workers, campaigns)  -> {root_id: {total, active, turns, wall_min, known}}
  grant(root_id, requested, usage, limits) -> (granted, reason)

`usage_from_status` derives the numbers from data that already exists (status.json worker rows
and campaigns.jsonl lines); nothing new is recorded. `grant` answers "how many more children may
this root add right now".

FAIL CLOSED ON UNKNOWN DATA. A usage that could not be derived (None, or a tree whose turn count
or start time cannot be read) is "cannot grant more", never "nothing used". A fresh root with no
workers and no campaign line is KNOWN to have used nothing, which is how a first split at depth 1
sees an empty tree and is granted exactly what it asked for.

DEFAULTS CHANGE NOTHING AT DEPTH 1. The first split of a root requests at most MAX_CHILDREN (12)
children against a total of 24 with one slot held back for the merge, a tree with no workers yet,
no turns and no wall clock, so the grant equals the request. tests/test_fanout_budget.py proves it
over every split size.

WHAT COUNTS. `total` is the workers that belong to the tree (children and merges, each carrying
the root's id; the top-level parent is the requester and carries none) or the children recorded
by campaign headers when more were queued than have become workers, whichever is larger. `active`
is workers that hold a tab (not finished, not waiting for admission). The ACTIVE limit bounds how
many of a tree's workers may be running when more are requested; queued children do not occupy
tabs, which is why a request is never trimmed to the concurrency cap.
"""
from __future__ import annotations

import json
import os
import time

#: The registry (tools/settings_keys.py) declares these four keys.
KEY_TOTAL = "fanout_max_total"
KEY_ACTIVE = "fanout_max_active"
KEY_TURNS = "fanout_max_turns"
KEY_WALL = "fanout_max_wall_min"

DEFAULT_MAX_TOTAL = 24
#: Equal to relay/fleet_runner.py DEFAULT_MAX_CONCURRENT (the maxtabs default), compared by a
#: test. When the key is absent the reader follows the operator's maxtabs instead.
DEFAULT_MAX_ACTIVE = 3
DEFAULT_MAX_TURNS = 400
DEFAULT_MAX_WALL_MIN = 120

#: (low, high) per key, applied on read. The panel clamps to the same bounds.
BOUNDS = {
    KEY_TOTAL: (2, 1000),
    KEY_ACTIVE: (1, 100),
    KEY_TURNS: (1, 1000000),
    KEY_WALL: (1, 10080),
}

#: Slots held back from the total for the merge worker that follows every split.
MERGE_RESERVE = 1

#: At most this many roots are exported in status.json.
MAX_ROOTS_EXPORTED = 20

#: Same list as relay/relay_fleet.py TERMINAL (compared by a test) plus the one status a live
#: worker holds before it has a tab.
_TERMINAL = ("done", "stuck", "maxturns", "error", "cancelled", "content_refused")
_NOT_YET_RUNNING = ("pending",)

#: The CPU bound on reading the ledger: a file past this is "unknown" (fail closed for nested
#: splits only). It was 2 MB, which a few dozen finished campaigns exceed, and it disabled
#: fan-out on every machine whose ledger had grown; retention caps the file at 64 MB.
_MAX_CAMPAIGN_BYTES = 50000000


def default_limits():
    return {"total": DEFAULT_MAX_TOTAL, "active": DEFAULT_MAX_ACTIVE,
            "turns": DEFAULT_MAX_TURNS, "wall_min": DEFAULT_MAX_WALL_MIN}


def _clamp(key, value, default):
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return default
    lo, hi = BOUNDS[key]
    return max(lo, min(hi, v))


def limits_from_settings():
    """The four limits, read from settings.txt on every call (each_gate). Never raises.

    An absent or unparsable key means its default; the active limit, when absent, follows the
    operator's maxtabs so it is never lower than the concurrency the fleet already runs at.
    """
    out = default_limits()
    try:
        from relay import fleet_runner as fr
        out["total"] = _clamp(KEY_TOTAL, fr._settings_int(KEY_TOTAL, None), DEFAULT_MAX_TOTAL)
        raw_active = fr._settings_int(KEY_ACTIVE, None)
        out["active"] = _clamp(KEY_ACTIVE, raw_active if raw_active is not None
                               else fr.settings_maxtabs(), DEFAULT_MAX_ACTIVE)
        out["turns"] = _clamp(KEY_TURNS, fr._settings_int(KEY_TURNS, None), DEFAULT_MAX_TURNS)
        out["wall_min"] = _clamp(KEY_WALL, fr._settings_int(KEY_WALL, None), DEFAULT_MAX_WALL_MIN)
    except Exception:
        return default_limits()
    return out


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _clean_limits(limits):
    d = default_limits()
    if not isinstance(limits, dict):
        return d
    for k, key in (("total", KEY_TOTAL), ("active", KEY_ACTIVE),
                   ("turns", KEY_TURNS), ("wall_min", KEY_WALL)):
        if k in limits:
            d[k] = _clamp(key, limits[k], d[k])
    return d


def _as_row(x):
    if isinstance(x, dict):
        return x
    if isinstance(x, (str, bytes)):
        try:
            v = json.loads(x)
        except (ValueError, TypeError):
            return None
        return v if isinstance(v, dict) else None
    return None


def rows_from_workers(workers):
    """Status-shaped rows from live worker objects: only what the budget reads."""
    out = []
    for w in workers or []:
        md = getattr(getattr(w, "task_envelope", None), "metadata", None) or {}
        rid = md.get("root_id") if isinstance(md, dict) else None
        out.append({"root_id": str(rid) if rid else "",
                    "status": getattr(w, "status", ""),
                    "turn": getattr(w, "turn", 0)})
    return out


def usage_from_status(workers, campaigns, now=None):
    """Per-root usage, or None when it cannot be derived at all.

    `workers`: status.json worker rows (dicts with root_id / status / turn).
    `campaigns`: campaigns.jsonl lines (dicts or JSON strings); [] for "no ledger", None for
    "ledger unreadable" (unknown). Returns {root_id: {total, active, turns, wall_min, known}}.
    `wall_min` is minutes since the root's first campaign header; None when the tree has workers
    but no readable header time, and `known` is then False.
    """
    if workers is None or campaigns is None:
        return None
    try:
        rows = list(workers)
    except TypeError:
        return None
    now = time.time() if now is None else now
    usage = {}

    def ent(rid):
        return usage.setdefault(rid, {"total": 0, "active": 0, "turns": 0, "wall_min": None,
                                      "known": True, "_hdr_n": 0, "_start": None})

    for r in rows:
        if not isinstance(r, dict):
            continue
        rid = r.get("root_id")
        if not rid:
            continue
        e = ent(str(rid))
        e["total"] += 1
        st = r.get("status")
        if st not in _TERMINAL and st not in _NOT_YET_RUNNING:
            e["active"] += 1
        t = r.get("turn")
        if _is_int(t) and t >= 0:
            e["turns"] += t
        else:
            e["known"] = False
    try:
        lines = list(campaigns)
    except TypeError:
        return None
    for ln in lines:
        c = _as_row(ln)
        if not c or c.get("kind") != "campaign":
            continue
        rid = c.get("root_id") or c.get("campaign_id")
        if not rid:
            continue
        e = ent(str(rid))
        n = c.get("n")
        if _is_int(n) and n > 0:
            e["_hdr_n"] += n
        ts = c.get("ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
            e["_start"] = ts if e["_start"] is None else min(e["_start"], ts)
    for e in usage.values():
        e["total"] = max(e["total"], e.pop("_hdr_n"))
        start = e.pop("_start")
        if start is not None:
            e["wall_min"] = round(max(0.0, now - start) / 60.0, 2)
        elif e["total"] > 0:
            e["known"] = False
    return usage


def grant(root_id, requested, usage, limits, self_active=0):
    """How many children `root_id` may add now: (granted, reason).

    `granted` is between 0 and `requested`; the caller enforces its own floor (a split needs at
    least two). `reason` is "" for a full grant and says which limit bound otherwise.
    `self_active` is how many of the root's active workers are the requester itself.
    """
    if not _is_int(requested) or requested <= 0:
        return 0, "nothing requested"
    lim = _clean_limits(limits)
    if not isinstance(usage, dict) or not root_id:
        return 0, "tree usage unknown"
    u = usage.get(root_id)
    if u is None:
        u = {"total": 0, "active": 0, "turns": 0, "wall_min": 0.0, "known": True}
    elif not isinstance(u, dict) or not u.get("known", False):
        return 0, "tree usage unknown"
    try:
        total, active = int(u["total"]), int(u["active"]) - int(self_active)
        turns, wall = int(u["turns"]), u["wall_min"]
    except (KeyError, TypeError, ValueError):
        return 0, "tree usage unknown"
    if wall is None:
        return 0, "tree usage unknown"
    if wall >= lim["wall_min"]:
        return 0, "tree wall clock %.0f min reached the %d min limit" % (wall, lim["wall_min"])
    if turns >= lim["turns"]:
        return 0, "tree turns %d reached the %d limit" % (turns, lim["turns"])
    if active >= lim["active"]:
        return 0, "tree has %d active workers (limit %d)" % (active, lim["active"])
    room = lim["total"] - total - MERGE_RESERVE
    if room <= 0:
        return 0, "tree has %d workers (limit %d)" % (total, lim["total"])
    if room < requested:
        return room, "tree worker limit %d leaves room for %d of %d" % (lim["total"], room, requested)
    return requested, ""


def trim_steps(steps, granted):
    """Cut `steps` to `granted` entries WITHOUT dropping work: the tail is folded into the last
    kept step. Unchanged (same list) when `granted` covers every step."""
    n = len(steps)
    if granted >= n:
        return steps
    if granted < 1:
        return []
    head = list(steps[:granted - 1])
    head.append("\n".join(str(s) for s in steps[granted - 1:]))
    return head


def apply_budget(steps, root_id, workers, campaigns, limits, min_children=2, now=None,
                 self_active=0):
    """The split decision: (steps_to_use, reason). `steps_to_use` is [] for "do not split".

    Pure over its inputs. A partial grant below `min_children` is a refusal, because one child
    is not a split.
    """
    if not steps:
        return steps, ""
    usage = usage_from_status(workers, campaigns, now=now)
    granted, why = grant(root_id, len(steps), usage, limits, self_active=self_active)
    if granted < min_children:
        return [], why or "fewer than %d children granted" % min_children
    return trim_steps(steps, granted), why


#: The only fields usage_from_status reads from a campaign header. The index keeps these and
#: drops the rest (the header's goal text can be thousands of characters).
_HEADER_KEYS = ("kind", "campaign_id", "root_id", "n", "ts")

#: path -> {"sig": (mtime_ns, size), "off": bytes consumed, "idx": {rid: [header rows]}}. The
#: ledger is append-only, so a grown file is read from `off` onward and a shrunk or replaced
#: one is read again from the start.
_INDEX_CACHE = {}
_INDEX_CACHE_MAX = 4


def _scan_headers(path, state):
    """Extend `state` (offset + index) with the lines appended since it was last read.

    Streams the file in binary, one line at a time; a line is parsed only when it can be a
    campaign header (it carries a "kind" key; child rows, merge rows and result rows do not), so
    the cost is about one substring test per line rather than one JSON parse. A final line with
    no newline yet (a write in progress) is left for the next call.
    """
    idx = state["idx"]
    with open(path, "rb") as fh:
        fh.seek(state["off"])
        off = state["off"]
        for raw in fh:
            if not raw.endswith(b"\n"):
                break
            off += len(raw)
            if b'"kind"' not in raw:
                continue
            ln = raw.decode("utf-8", "replace")
            if off == len(raw) and ln.startswith("﻿"):
                ln = ln[1:]
            c = _as_row(ln)
            if not c or c.get("kind") != "campaign":
                continue
            rid = c.get("root_id") or c.get("campaign_id")
            if not rid:
                continue
            idx.setdefault(str(rid), []).append({k: c[k] for k in _HEADER_KEYS if k in c})
        state["off"] = off


def _header_index(path):
    """The campaign-header index of `path`, or None when it cannot be built (unreadable, or
    past _MAX_CAMPAIGN_BYTES, the CPU bound). Cached by (mtime, size), appended incrementally."""
    st = os.stat(path)
    sig = (st.st_mtime_ns, st.st_size)
    if st.st_size > _MAX_CAMPAIGN_BYTES:
        return None
    key = os.path.normcase(os.path.abspath(path))
    state = _INDEX_CACHE.get(key)
    if state is not None and state["sig"] == sig:
        return state["idx"]
    if state is None or st.st_size < state["off"] or state["sig"][1] > st.st_size:
        state = {"sig": sig, "off": 0, "idx": {}}
    _scan_headers(path, state)
    state["sig"] = sig
    _INDEX_CACHE.pop(key, None)
    _INDEX_CACHE[key] = state
    while len(_INDEX_CACHE) > _INDEX_CACHE_MAX:
        _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)))
    return state["idx"]


def read_campaign_rows(path, root_ids=None):
    """Campaign header rows from campaigns.jsonl: [] when there is no file, None when it exists
    but cannot be read (unknown, so a caller fails closed).

    The ledger is STREAMED, never loaded whole, and its size no longer turns into "unknown" until
    _MAX_CAMPAIGN_BYTES (50 MB), far above where a ledger sits under retention. Only header rows
    are returned (the only rows usage_from_status reads), reduced to the fields it uses.
    `root_ids` (an iterable of ids) narrows the result to exactly those roots; None returns every
    header. A row belongs to root_id when its root_id is that id, or, with no root_id, when its
    campaign_id is: an id that merely contains another is a different root.
    """
    if not path or not os.path.exists(path):
        return []
    try:
        idx = _header_index(path)
    except Exception:
        return None
    if idx is None:
        return None
    if root_ids is None:
        return [r for rows in idx.values() for r in rows]
    return [r for rid in {str(x) for x in root_ids} for r in idx.get(rid, ())]


def ledger_rows_for_split(path, root_id):
    """The campaign rows a split decision needs, as read_campaign_rows returns them.

    A TOP-LEVEL split (`root_id` empty) is its own brand-new root: no worker carries its id and
    no header of it exists yet, so its recorded usage is zero by construction and the ledger is
    not read, whatever its size or state. A NESTED split reads only its own root's rows, and None
    (unreadable) refuses that split alone, via grant()'s fail-closed "tree usage unknown".
    """
    if not root_id:
        return []
    return read_campaign_rows(path, root_ids=[root_id])


def status_block(workers, campaigns, limits=None, now=None):
    """The additive status.json export: {"tree_budget": {root: used+limits}, "fanout_budget": limits}.

    Roots are capped at MAX_ROOTS_EXPORTED (the most-used first). {} when usage cannot be derived,
    so a reader sees "not reported" rather than a comfortable zero.
    """
    lim = _clean_limits(limits if limits is not None else limits_from_settings())
    usage = usage_from_status(workers, campaigns, now=now)
    if usage is None:
        return {}
    # Trees that have a worker now come first: a ledger full of finished campaigns must not
    # crowd the live tree out of the capped export.
    live = {str(r.get("root_id")) for r in (workers or []) if isinstance(r, dict) and r.get("root_id")}
    ranked = sorted(usage.items(),
                    key=lambda kv: (kv[0] not in live, -kv[1]["total"], kv[0]))[:MAX_ROOTS_EXPORTED]
    return {"fanout_budget": dict(lim),
            "tree_budget": {rid: {"total": e["total"], "active": e["active"], "turns": e["turns"],
                                  "wall_min": e["wall_min"], "known": e["known"],
                                  "limits": dict(lim)} for rid, e in ranked}}


__all__ = ["grant", "usage_from_status", "apply_budget", "trim_steps", "limits_from_settings",
           "status_block", "read_campaign_rows", "ledger_rows_for_split", "rows_from_workers", "default_limits"]
