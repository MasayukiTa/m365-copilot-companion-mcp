# -*- coding: utf-8 -*-
"""A per-tree budget for fan-out: how many workers, how much work, how long one root may use.

WHY. A fan-out tree is a root campaign and everything split from it (every worker carries the
tree's `root_id`). The default depth is 2 when hierarchical merging is on, and one split
yields at most MAX_CHILDREN. Per-tree budgets bound resource use independently of tree shape.

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

DEFAULTS DO NOT RESTRICT THE FIRST SPLIT. The first split of a root requests at most MAX_CHILDREN (12)
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

#: MEASURED BASIS (last 7 days of this fleet's .fleet/campaigns.jsonl + status.json phase_events,
#: under the CORRECTED counters this module now uses; see explain.md measured-distributions):
#: a healthy completed tree holds p90 9 / max 12 workers that actually ran, so with the merge
#: worker and the top-level parent it stays well under 24. 24 keeps the cap above the largest
#: single split (MAX_CHILDREN 12 + MERGE_RESERVE + parent) while still catching a runaway
#: recursive fan-out. Kept at 24 (keys/bounds/settings screen are owner-controlled).
DEFAULT_MAX_TOTAL = 24
#: Equal to the registry's fanout_max_active default (tools/settings_keys.py), compared by a
#: test. It is the fallback for an unreadable settings file; when the key is absent the reader
#: follows the operator's maxtabs instead (bounded by BOUNDS), which is no longer 3.
DEFAULT_MAX_ACTIVE = 3
DEFAULT_MAX_TURNS = 400
#: MEASURED BASIS (phase_events active spans, n=1480 workers): per-worker active time is
#: median 13 min, p95 69 min, p99 120 min, max 221 min; only 0.2% of workers exceed 180 min and
#: none exceed 240. A tree's ACTIVE span (now measured first-worker-start -> last-worker-end,
#: frozen once all workers are terminal -- no longer an ever-growing clock) is bounded by its
#: longest concurrent worker, so a healthy tree finishes well under 180 min. PROPOSED default 180
#: (clears the worker p99 with margin yet still catches a tree wedged in a retry/stuck loop).
#: Kept at 120 here so the registry, this reader and the cockpit Defaults stay in lockstep (three
#: cross-checking tests); the owner raises the live value to 180 in the cockpit settings.
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

#: How long a campaign header with NO worker row yet and NO resolved merge may still be treated as
#: "about to split" (live: its clock runs to now and its header n floors the total). This covers the
#: brief window where a split has been written to the ledger but its children have not yet surfaced
#: as worker rows. Past this grace the header is an ORPHAN -- a split whose workers never appeared
#: (coordinator died mid-split, merge row never written, etc.). Such a header must NOT keep counting
#: header-start -> now: that is the weeks-long (27000-45000 min) wall times and never-shrinking
#: totals seen on long-abandoned roots. A healthy split surfaces workers in minutes and a healthy
#: tree finishes well under the wall limit, so reusing DEFAULT_MAX_WALL_MIN keeps this in lockstep
#: with the wall gate: if no worker has appeared within one full wall budget, the header is dead.
ORPHAN_HEADER_GRACE_MIN = DEFAULT_MAX_WALL_MIN

#: At most this many roots are exported in status.json.
MAX_ROOTS_EXPORTED = 20

#: Same list as relay/relay_fleet.py TERMINAL (compared by a test) plus the one status a live
#: worker holds before it has a tab.
_TERMINAL = ("done", "stuck", "maxturns", "error", "cancelled", "content_refused")
_NOT_YET_RUNNING = ("pending",)

#: There is NO size gate on reading the ledger. It was 2 MB (disabled fan-out on every machine
#: whose ledger had grown), then 50 MB, but retention does not compact campaigns.jsonl
#: (fleet_retention only warns), so any byte cap eventually turns a long-lived machine into
#: "usage unknown" and refuses every nested split. The read is streamed and cached by
#: (mtime, size) and appended incrementally, so its cost is the file's growth, not its size.


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


#: phase_events whose `event` marks a worker only WAITING for admission, never holding a tab.
#: The active span must not start here: time a worker spends queued or waiting to be let in is
#: not time the tree was doing work. (`pending` is the queued state in status.json phase_events;
#: the others are defensive synonyms seen in older snapshots.)
_ADMISSION_WAIT = ("pending", "queued", "waiting_admission", "admission_wait")


def _span_from_phase_events(events):
    """(started, ended) wall-clock ts for ONE worker, from its status phase_events.

    `started` is the ts of the first event that is NOT an admission/queue wait -- the first time
    the worker actually held a tab and did work. `ended` is the ts of the last TERMINAL event, or
    None while the worker is still live. Measuring from the first non-wait event means a tree's
    active span excludes the time its workers only waited to be admitted or to get a send token.
    """
    started = ended = None
    if not isinstance(events, (list, tuple)):
        return None, None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        ts = ev.get("ts")
        if not (isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0):
            continue
        name = ev.get("event")
        if name not in _ADMISSION_WAIT:
            if started is None or ts < started:
                started = ts
        if name in _TERMINAL:
            if ended is None or ts > ended:
                ended = ts
    return started, ended


def rows_from_workers(workers):
    """Status-shaped rows from live worker objects: only what the budget reads.

    Also carries each worker's ACTIVE span (`started` / `ended` wall-clock ts) derived from its
    phase_events, so usage_from_status can measure a tree's active wall time (first worker start
    to last worker end) instead of time since the tree's first campaign header.
    """
    out = []
    for w in workers or []:
        md = getattr(getattr(w, "task_envelope", None), "metadata", None) or {}
        rid = md.get("root_id") if isinstance(md, dict) else None
        started, ended = _span_from_phase_events(getattr(w, "phase_events", None))
        out.append({"root_id": str(rid) if rid else "",
                    "status": getattr(w, "status", ""),
                    "turn": getattr(w, "turn", 0),
                    "started": started, "ended": ended})
    return out


def usage_from_status(workers, campaigns, now=None):
    """Per-root usage, or None when it cannot be derived at all.

    `workers`: status.json worker rows (dicts with root_id / status / turn, and -- when available
    -- the worker's active span as `started` / `ended` wall-clock ts from its phase_events).
    `campaigns`: campaigns.jsonl lines (dicts or JSON strings); [] for "no ledger", None for
    "ledger unreadable" (unknown). Returns {root_id: {total, active, turns, wall_min, known}}.

    `total` COUNTS THE WORKERS THAT ACTUALLY RAN for the tree (its children and merge worker),
    not the running sum of every past split's campaign-header `n`. That header sum only ever grows
    -- it is never decremented when children finish -- so an aged, finished tree used to report
    e.g. total 28 or 128 workers it no longer has. The header `n` is kept only as a FLOOR while the
    tree is still LIVE and some of its requested children are queued but not yet workers, which is
    how a first split still sees the size it asked for.

    `wall_min` is the tree's ACTIVE SPAN in minutes: from its first worker's start to its last
    worker's end, measured from phase_events (admission/queue waiting is not the start). Once every
    worker of the tree is terminal the clock FREEZES at the last worker's end -- it does not keep
    counting against `now`, so a finished tree no longer shows an ever-growing wall clock (the
    41183-min / 525-min trees). While any worker is still live the end is `now`. When no per-worker
    timing is present the span falls back to the first campaign-header ts -> now while the tree is
    live OR has no worker rows yet (a split being decided from the ledger alone, ran==0); a tree
    whose workers all ran and finished (ran>0, none live) reports 0.0 ("not running").
    `known` is False only when a counted worker's turn, or a live tree's start, cannot be read.
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
                                      "known": True, "_hdr_n": 0, "_hdr_start": None,
                                      "_hdr_last": None,
                                      "_ran": 0, "_live": False, "_fin": False,
                                      "_wstart": None, "_wend": None})

    for r in rows:
        if not isinstance(r, dict):
            continue
        rid = r.get("root_id")
        if not rid:
            continue
        e = ent(str(rid))
        e["_ran"] += 1
        st = r.get("status")
        if st not in _TERMINAL and st not in _NOT_YET_RUNNING:
            e["active"] += 1
            e["_live"] = True
        t = r.get("turn")
        if _is_int(t) and t >= 0:
            e["turns"] += t
        else:
            e["known"] = False
        # Per-worker active span. rows_from_workers (live worker objects) precomputes started/
        # ended; a status.json worker row (the production export, _tree_budget_block) instead
        # carries phase_events, so derive the span from those here. Without this, every tree that
        # reaches the export fell back to header-start->now and the active-span fix never ran in
        # production -- which is exactly how finished trees kept an ever-growing wall clock.
        ws = r.get("started")
        we = r.get("ended")
        if ws is None and we is None:
            ws, we = _span_from_phase_events(r.get("phase_events"))
        if isinstance(ws, (int, float)) and not isinstance(ws, bool) and ws > 0:
            e["_wstart"] = ws if e["_wstart"] is None else min(e["_wstart"], ws)
        if isinstance(we, (int, float)) and not isinstance(we, bool) and we > 0:
            e["_wend"] = we if e["_wend"] is None else max(e["_wend"], we)
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
        if c.get("_finished"):
            e["_fin"] = True
        n = c.get("n")
        if _is_int(n) and n > 0:
            e["_hdr_n"] += n
        ts = c.get("ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
            e["_hdr_start"] = ts if e["_hdr_start"] is None else min(e["_hdr_start"], ts)
            e["_hdr_last"] = ts if e["_hdr_last"] is None else max(e["_hdr_last"], ts)
    for e in usage.values():
        ran = e.pop("_ran")  # noqa: F841 (read below for the wall-clock branch)
        hdr_n = e.pop("_hdr_n")
        live = e.pop("_live")
        fin = e.pop("_fin")
        hdr_last = e.pop("_hdr_last")
        # A resolved merge (its row is in the ledger) ENDS the tree, even if a stale worker row in
        # status.json has not yet flipped to a terminal state and still looks "live". Without this
        # override such a tree keeps its active span open to now and accrues days-to-weeks of wall
        # time (the 27000-45000 min runaways). `fin` therefore wins over the worker-derived `live`.
        live = live and not fin
        # A tree is treated as "about to split" (header count is a floor, clock runs to now) only
        # when it is live, or when it has no worker row yet AND its merge has not resolved AND its
        # newest header is still within the orphan grace. A FINISHED tree (its merge row is in the
        # ledger) whose workers have since dropped out of status.json would otherwise fall into the
        # ran == 0 branch and keep counting header-start -> now forever -- the stale wall times
        # (hours to weeks) seen on long-done trees. Treating it as finished freezes its clock and
        # stops flooring its total by every past split.
        #
        # The grace also catches the ORPHAN header: a split whose children never surfaced as worker
        # rows and whose merge was never written (coordinator died mid-split). It has ran == 0 and
        # fin is False -- identical to a split still being decided -- so neither `live` nor `fin`
        # can distinguish it. Only its AGE can: a real pending split surfaces workers in minutes,
        # while an orphan's header is hours-to-weeks old. Past ORPHAN_HEADER_GRACE_MIN we stop
        # treating it as about-to-split, which freezes its clock (wall_min 0.0) and drops its total
        # to the workers that actually ran (0), instead of header-start -> now forever.
        hdr_fresh = (hdr_last is not None
                     and (now - hdr_last) <= ORPHAN_HEADER_GRACE_MIN * 60)
        about_to_split = live or (ran == 0 and not fin and hdr_fresh)
        # Workers that actually ran. The header count is a FLOOR in exactly two cases: the tree is
        # still live and more children were requested than have become workers (queued children do
        # not yet show as rows), or no worker row exists yet at all (ran == 0) -- a split being
        # decided from the ledger alone, before its children become workers. A FINISHED tree whose
        # workers have run (ran > 0, none live) is NOT floored by the header sum, so its total no
        # longer grows with every past split it once made.
        e["total"] = max(ran, hdr_n) if (about_to_split and hdr_n > ran) else ran
        wstart = e.pop("_wstart")
        wend = e.pop("_wend")
        hdr_start = e.pop("_hdr_start")
        if wstart is not None:
            # Active span: first worker start -> last worker end; freeze once all workers terminal.
            end = now if live else (wend if wend is not None else wstart)
            e["wall_min"] = round(max(0.0, end - wstart) / 60.0, 2)
        elif fin and (hdr_start is not None):
            # Finished tree, no per-worker timing: freeze from first to last header, never to now.
            end = hdr_last if hdr_last is not None else hdr_start
            e["wall_min"] = round(max(0.0, end - hdr_start) / 60.0, 2)
        elif about_to_split and hdr_start is not None:
            # No per-worker timing yet but the tree is live: header start -> now.
            e["wall_min"] = round(max(0.0, now - hdr_start) / 60.0, 2)
        elif about_to_split:
            # Live (or a header-only tree about to split) but start unreadable -> fail closed.
            e["known"] = False
        elif e["total"] > 0 or hdr_n > 0:
            # Not live (finished or aged): the clock has stopped. No ever-growing wall time.
            e["wall_min"] = 0.0
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

#: Ledger rows that mark a whole tree's merge as RESOLVED, so its wall clock has stopped. A root
#: that has one of these (and no live worker) is finished: usage_from_status freezes its wall
#: time instead of counting from the tree's first header to now. `merge_requeued` is NOT here --
#: a requeued merge is still pending, the tree is not done.
_TREE_DONE = ("merged", "merge_done", "merge_abandoned")

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
    done = state["done"]
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
            if not c:
                continue
            kind = c.get("kind")
            if kind in _TREE_DONE:
                # A tree's merge has resolved: its clock has stopped. root_id names the tree; a
                # merge row without one is keyed by its own campaign_id (the root of its tree).
                drid = c.get("root_id") or c.get("campaign_id")
                if drid:
                    done.add(str(drid))
                continue
            if kind != "campaign":
                continue
            rid = c.get("root_id") or c.get("campaign_id")
            if not rid:
                continue
            idx.setdefault(str(rid), []).append({k: c[k] for k in _HEADER_KEYS if k in c})
        state["off"] = off


def _header_index(path):
    """The scan state of `path`: {"idx": {rid: [header rows]}, "done": {finished rids}, ...}.
    The caller turns an OSError into "unknown"; the file's size never does. Cached by (mtime,
    size), appended incrementally."""
    st = os.stat(path)
    sig = (st.st_mtime_ns, st.st_size)
    key = os.path.normcase(os.path.abspath(path))
    state = _INDEX_CACHE.get(key)
    if state is not None and state["sig"] == sig:
        return state
    if state is None or st.st_size < state["off"] or state["sig"][1] > st.st_size:
        state = {"sig": sig, "off": 0, "idx": {}, "done": set()}
    _scan_headers(path, state)
    state["sig"] = sig
    _INDEX_CACHE.pop(key, None)
    _INDEX_CACHE[key] = state
    while len(_INDEX_CACHE) > _INDEX_CACHE_MAX:
        _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)))
    return state


def read_campaign_rows(path, root_ids=None):
    """Campaign header rows from campaigns.jsonl: [] when there is no file, None when it exists
    but cannot be read (unknown, so a caller fails closed).

    The ledger is STREAMED, never loaded whole, and its size no longer turns into "unknown" until
    any byte cap (there is none). Only header rows
    are returned (the only rows usage_from_status reads), reduced to the fields it uses.
    `root_ids` (an iterable of ids) narrows the result to exactly those roots; None returns every
    header. A row belongs to root_id when its root_id is that id, or, with no root_id, when its
    campaign_id is: an id that merely contains another is a different root.
    """
    if not path or not os.path.exists(path):
        return []
    try:
        state = _header_index(path)
    except Exception:
        return None
    if state is None:
        return None
    idx = state["idx"]
    done = state.get("done") or set()

    def rows_of(rid):
        # A finished tree (its merge resolved) carries `_finished` on each header so
        # usage_from_status freezes its wall clock instead of counting to now. The flag is added
        # to a copy; the cached header dict is never mutated.
        fin = rid in done
        for r in idx.get(rid, ()):
            yield dict(r, _finished=True) if fin else r

    if root_ids is None:
        return [r for rid in idx for r in rows_of(rid)]
    return [r for rid in {str(x) for x in root_ids} for r in rows_of(rid)]


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
