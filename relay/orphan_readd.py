# -*- coding: utf-8 -*-
"""Re-queue goals that LANDED but were dropped by a cold start.

WHY THIS EXISTS. Measured 2026-11-10: every time the stack is cold started (05:10 and 14:08
that day), every goal that had been acknowledged -- 'landed': .fleet/acks/<jid>.ack and
.fleet/tasks/done/<jid>.landed.json both exist -- but had produced no outcome file was silently
dropped. status.json after the restart read workers=1 total=1, while the owners of five
ShuttleScope goals and several orchestrator goals waited for hours with no worker and no
outcome. The admission survived on disk (the intake journal .fleet/intake_journal.jsonl and
for_fleet/<jid>.txt still held what was submitted); nothing re-queued it.

WHAT THIS DOES. On runner start, compute the set of ACKNOWLEDGED jids that have NO outcome file,
NO delivered record, NO live worker, NO refused record, and whose ack is not older than the age
limit, and re-add each to the queue exactly once, carrying a marker field
``resubmitted_after_restart``. A jid that already has an outcome, a delivered record, a live
worker, a refused record, or an ack older than the age limit is NEVER re-added. ONE summary line
is emitted per start carrying the counts (re-added / skipped delivered / skipped too old /
skipped no text) -- never one line per stale jid -- and the re-added count is recorded so
status.json can surface it.

WHERE EACH SIGNAL LIVES (the same layout the rest of the fleet already joins on by jid):
  * acknowledged      .fleet/acks/<jid>.ack                     (landing receipt; mtime ages it)
  * outcome           .fleet/tasks/done/<jid>.outcome.json      (the job finished; terminal)
  * delivered         .fleet/tasks/done/<jid>.delivered.json    (the result was delivered; terminal
                      exactly like an outcome, even when no outcome.json was written)
  * refused           .fleet/tasks/done/<jid>.landed.json with status == "refused"
                      (the fleet READ the add_goal and refused it -- a real decision, not a drop)
  * live worker       status.json workers[].jid                 (a worker already carries it)
  * too old           ack mtime older than ORPHAN_MAX_AGE_SECONDS (its done-dir record may have
                      been pruned by retention, leaving no trace to judge it)
  * goal text         for_fleet/<jid>.txt, else the intake journal's preview

The re-add writes the goal back into for_fleet/<jid>.txt -- the same parked-goal channel the
router and autostart-recovery already use -- as a JSON object that carries the marker. A plain
parked goal stays plain; only a resubmission carries the marker, so `read_for_fleet` (which
returns the ``text`` and ignores extra keys) keeps reading both shapes unchanged.

IDEMPOTENT ACROSS STARTS BY CONSTRUCTION. The re-add parks the goal in for_fleet/, but the
*decision* to re-add is gated on the absence of an outcome / worker / refusal AND on the goal
not already sitting in for_fleet/ as a resubmission -- so a second start that finds the same
orphan still parked (not yet consumed) re-adds nothing. Once the fleet consumes the parked goal
and records any terminal outcome, the first gate already excludes it.

NEVER RAISES ONTO THE RUN. A start must not be failed by this housekeeping: every disk read is
guarded and a failure degrades to "re-added nothing", logged, never propagated.
"""
from __future__ import annotations

import json
import os
import time

#: The marker every resubmission carries, both as the for_fleet JSON field name and its value's
#: witness. A reader can tell a restart re-add from an ordinary parked goal by this key alone.
RESUBMIT_MARKER = "resubmitted_after_restart"

#: An ack older than this (by the ack file's mtime) is never re-added. Two cold starts dropped
#: goals acked within a few hours of the restart, so a window of a couple of days keeps those
#: recoverable; but acks older than the done-dir retention have had their landing records pruned,
#: leaving no trace to judge landed-vs-refused-vs-finished -- re-adding them blind would flood
#: the queue with goals that may already be done. 48h sits above the recent-incident window and
#: below that retention. Overridable via the FLEET_ORPHAN_MAX_AGE_SECONDS environment variable.
ORPHAN_MAX_AGE_SECONDS = 48 * 60 * 60

#: Where the count/jids of the last start's re-adds are recorded, so the status snapshot can
#: surface them without re-walking acks/ on the sweep thread. Shape:
#:   {"count": int, "jids": [str, ...], "at": float}
ORPHAN_READD_RECORD = "orphan_readd.json"

ACKS_DIR = "acks"


def _acked_jids(state_dir):
    """Every jid with a landing receipt under <state_dir>/acks/<jid>.ack."""
    out = []
    acks = os.path.join(state_dir, ACKS_DIR)
    try:
        names = os.listdir(acks)
    except OSError:
        return out
    for name in names:
        if name.endswith(".ack"):
            jid = name[:-4]
            if jid:
                out.append(jid)
    return out


def _max_age_seconds():
    """The age limit in effect: FLEET_ORPHAN_MAX_AGE_SECONDS if set to a positive number, else the
    ORPHAN_MAX_AGE_SECONDS default. A malformed or non-positive override falls back to the default
    rather than disabling the guard."""
    raw = os.environ.get("FLEET_ORPHAN_MAX_AGE_SECONDS", "").strip()
    if raw:
        try:
            val = float(raw)
        except ValueError:
            return ORPHAN_MAX_AGE_SECONDS
        if val > 0:
            return val
    return ORPHAN_MAX_AGE_SECONDS


def _ack_mtime(state_dir, jid):
    """The mtime of this jid's ack file, or None when it cannot be read."""
    try:
        return os.path.getmtime(os.path.join(state_dir, ACKS_DIR, "%s.ack" % jid))
    except OSError:
        return None


def _is_too_old(state_dir, jid, now=None, max_age=None):
    """True when the ack is older than the age limit. An unreadable mtime is treated as NOT too old
    (fail open to the other terminal gates), so a jid is only aged out on positive evidence."""
    mtime = _ack_mtime(state_dir, jid)
    if mtime is None:
        return False
    if now is None:
        now = time.time()
    if max_age is None:
        max_age = _max_age_seconds()
    return (now - mtime) > max_age


def _has_outcome(state_dir, jid):
    """True when <state_dir>/tasks/done/<jid>.outcome.json exists -- the job reached a terminal
    outcome and must never be re-added."""
    return os.path.isfile(os.path.join(state_dir, "tasks", "done", "%s.outcome.json" % jid))


def _is_delivered(state_dir, jid):
    """True when <state_dir>/tasks/done/<jid>.delivered.json exists -- the goal finished and its
    result was delivered. Terminal exactly like an outcome file: a delivered goal must never be
    re-added, even if no outcome.json was written."""
    return os.path.isfile(os.path.join(state_dir, "tasks", "done", "%s.delivered.json" % jid))


def _is_refused(state_dir, jid):
    """True when the landing record marks this jid refused. A refusal is a real decision by the
    command channel (it READ the add_goal and declined it), not a drop, so it is terminal for
    the purpose of re-adding."""
    path = os.path.join(state_dir, "tasks", "done", "%s.landed.json" % jid)
    try:
        with open(path, encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return False
    return isinstance(rec, dict) and rec.get("status") == "refused"


def _live_worker_jids(state_dir):
    """The jids a worker in the current status.json already carries. A jid here is being worked
    on and must not be re-added. Missing/unreadable status.json -> no live workers (the common
    cold-start case this module exists for)."""
    out = set()
    path = os.path.join(state_dir, "status.json")
    try:
        with open(path, encoding="utf-8-sig") as fh:
            snap = json.load(fh)
    except (OSError, ValueError):
        return out
    for w in (snap or {}).get("workers") or []:
        if isinstance(w, dict):
            jid = str(w.get("jid") or "").strip()
            if jid:
                out.add(jid)
    return out


def _already_resubmitted_in_for_fleet(state_dir, jid):
    """True when for_fleet/<jid>.txt is already a resubmission carrying the marker -- so a second
    start that finds the same orphan still parked (not yet consumed) does not park it again."""
    path = os.path.join(state_dir, "for_fleet", "%s.txt" % jid)
    try:
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
    except OSError:
        return False
    body = (body or "").lstrip()
    if not body.startswith("{"):
        return False
    try:
        obj = json.loads(body)
    except ValueError:
        return False
    return isinstance(obj, dict) and bool(obj.get(RESUBMIT_MARKER))


def _goal_text_for(state_dir, jid):
    """The goal text to re-queue for `jid`: the parked for_fleet file if it still holds one, else
    the intake journal's preview for that jid. None when neither can supply text."""
    path = os.path.join(state_dir, "for_fleet", "%s.txt" % jid)
    try:
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
    except OSError:
        body = ""
    body = body or ""
    if body.strip():
        stripped = body.lstrip()
        if stripped.startswith("{"):
            try:
                obj = json.loads(stripped)
            except ValueError:
                obj = None
            if isinstance(obj, dict) and isinstance(obj.get("text"), str) and obj["text"].strip():
                return obj["text"]
        else:
            return body
    # Fall back to the intake journal preview -- the one place that records what was submitted
    # for every route, joined by jid.
    try:
        from relay import intake_journal as _ij
        for rec in _ij.read_all(state_dir):
            if isinstance(rec, dict) and str(rec.get("jid") or "") == jid:
                text = rec.get("goal") or ""
                if text.strip():
                    return text
    except Exception:
        pass
    return None


def orphan_jids(state_dir, buckets=None):
    """The acknowledged jids that were dropped: ack present, but NO outcome, NOT delivered, NO live
    worker, NO refused record, and NOT older than the age limit. Deterministic order (sorted) so
    the log and the status record are stable.

    This is the pure decision -- it writes nothing. `resubmit_orphans_on_start` calls it to pick
    the goals it re-adds, passing the `_classify` result it already computed so the acks/ tree is
    walked once per start, not twice.
    """
    if buckets is None:
        buckets = _classify(state_dir)
    return sorted(set(buckets["orphans"]))


def _classify(state_dir):
    """Walk every acked jid once and bucket it. Returns a dict with the orphan jids and the counts
    of each terminal/excluded reason, so a start can emit a single summary line instead of one
    line per stale jid. 'delivered' and 'too_old' are the two new guards; a jid is counted under
    the first reason that excludes it (live/outcome/delivered/refused/too_old), so the buckets do
    not double-count."""
    live = _live_worker_jids(state_dir)
    now = time.time()
    max_age = _max_age_seconds()
    orphans = []
    skipped_delivered = 0
    skipped_too_old = 0
    for jid in _acked_jids(state_dir):
        if jid in live:
            continue
        if _has_outcome(state_dir, jid):
            continue
        if _is_delivered(state_dir, jid):
            skipped_delivered += 1
            continue
        if _is_refused(state_dir, jid):
            continue
        if _is_too_old(state_dir, jid, now=now, max_age=max_age):
            skipped_too_old += 1
            continue
        orphans.append(jid)
    return {
        "orphans": orphans,
        "skipped_delivered": skipped_delivered,
        "skipped_too_old": skipped_too_old,
    }


def _record(state_dir, jids):
    """Record the count/jids of this start's re-adds for the status snapshot. Never raises."""
    try:
        payload = {"count": len(jids), "jids": list(jids), "at": time.time()}
        tmp = os.path.join(state_dir, ORPHAN_READD_RECORD + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False))
        os.replace(tmp, os.path.join(state_dir, ORPHAN_READD_RECORD))
    except Exception:
        pass


def read_record(state_dir):
    """The last start's re-add record ({count, jids, at}), or {} when none/unreadable. For the
    status snapshot block."""
    try:
        with open(os.path.join(state_dir, ORPHAN_READD_RECORD), encoding="utf-8") as fh:
            rec = json.load(fh)
        return rec if isinstance(rec, dict) else {}
    except (OSError, ValueError):
        return {}


def resubmit_orphans_on_start(state_dir, log=None, park=None):
    """Re-add every dropped-after-ack goal to the queue exactly once, carrying the marker.

    Returns the list of jids re-added (possibly empty). Records the count for status.json and
    emits one `log` line per re-added jid. NEVER raises: any failure degrades to re-adding
    nothing and is reported through `log`.

    `log`  -- a callable taking one string (defaults to print). `park` -- a callable
    (jid, text, marker_obj) -> bool that writes the goal into the queue (defaults to the
    for_fleet channel via relay.task_router._write_for_fleet); injectable so a test need not
    stand up the whole router.
    """
    if log is None:
        def log(msg):
            print(msg, flush=True)
    if park is None:
        park = _park_via_for_fleet

    readded = []
    try:
        buckets = _classify(state_dir)
        candidates = orphan_jids(state_dir, buckets)
    except Exception as exc:                       # housekeeping must not fail a start
        log("orphan re-add skipped: %s: %s" % (type(exc).__name__, exc))
        return readded

    skipped_delivered = buckets["skipped_delivered"]
    skipped_too_old = buckets["skipped_too_old"]
    skipped_no_text = 0
    for jid in candidates:
        try:
            if _already_resubmitted_in_for_fleet(state_dir, jid):
                continue                           # still parked from a prior start; not twice
            text = _goal_text_for(state_dir, jid)
            if not text:
                skipped_no_text += 1
                continue
            marker = {RESUBMIT_MARKER: True, "jid": jid}
            if park(jid, text, marker):
                readded.append(jid)
            else:
                skipped_no_text += 1
        except Exception as exc:
            log("orphan re-add: %s skipped: %s: %s" % (jid, type(exc).__name__, exc))

    _record(state_dir, readded)
    # One summary line per start, never one line per stale jid: the 1000+ long-pruned acks that
    # age out would otherwise flood the log on every start.
    log("orphan re-add summary: re-added=%d skipped_delivered=%d skipped_too_old=%d "
        "skipped_no_text=%d" % (len(readded), skipped_delivered, skipped_too_old, skipped_no_text))
    return readded


def _park_via_for_fleet(jid, text, marker):
    """Write the goal back into for_fleet/<jid>.txt as a marked resubmission, via the router's
    own atomic shape so a reader never sees half a goal. The marker travels as extra keys the
    reader ignores; `text`/`priority` keep their established meaning. The state dir is resolved
    from FLEET_STATE_DIR exactly like the router itself."""
    from relay import task_router as _tr
    env = os.environ.get("FLEET_STATE_DIR", "").strip()
    sd = env or getattr(_tr, "FLEET_STATE_DIR", None)
    if not sd:
        return False
    _tr.ensure_dirs()
    obj = {"text": text, "priority": False}
    obj.update(marker)
    body = json.dumps(obj, ensure_ascii=False)
    base = os.path.join(sd, "for_fleet")
    path = os.path.join(base, "%s.txt" % jid)
    tmp = os.path.join(base, "%s.tmp" % jid)
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
