# -*- coding: utf-8 -*-
"""Re-queue goals that LANDED but were dropped by a cold start.

WHY THIS EXISTS. Measured 2026-11-10: every time the stack is cold started (05:10 and 14:08
that day), every goal that had been acknowledged -- 'landed': .fleet/acks/<jid>.ack and
.fleet/tasks/done/<jid>.landed.json both exist -- but had produced no outcome file was silently
dropped. status.json after the restart read workers=1 total=1, while the owners of five
ShuttleScope goals and several orchestrator goals waited for hours with no worker and no
outcome. The admission survived on disk (the intake journal .fleet/intake_journal.jsonl and
for_fleet/<jid>.txt still held what was submitted); nothing re-queued it.

WHAT THIS DOES. On runner start, compute the set of ACKNOWLEDGED jids that have NO outcome file
and NO live worker, and re-add each to the queue exactly once, carrying a marker field
``resubmitted_after_restart``. A jid that already has an outcome, a live worker, or a refused
record is NEVER re-added. One log line is emitted per re-added jid and the count is recorded so
status.json can surface it.

WHERE EACH SIGNAL LIVES (the same layout the rest of the fleet already joins on by jid):
  * acknowledged      .fleet/acks/<jid>.ack                     (landing receipt)
  * outcome           .fleet/tasks/done/<jid>.outcome.json      (the job finished; terminal)
  * refused           .fleet/tasks/done/<jid>.landed.json with status == "refused"
                      (the fleet READ the add_goal and refused it -- a real decision, not a drop)
  * live worker       status.json workers[].jid                 (a worker already carries it)
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


def _has_outcome(state_dir, jid):
    """True when <state_dir>/tasks/done/<jid>.outcome.json exists -- the job reached a terminal
    outcome and must never be re-added."""
    return os.path.isfile(os.path.join(state_dir, "tasks", "done", "%s.outcome.json" % jid))


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


def orphan_jids(state_dir):
    """The acknowledged jids that were dropped: ack present, but NO outcome, NO live worker, and
    NO refused record. Deterministic order (sorted) so the log and the status record are stable.

    This is the pure decision -- it writes nothing. `resubmit_orphans_on_start` acts on it.
    """
    live = _live_worker_jids(state_dir)
    out = []
    for jid in _acked_jids(state_dir):
        if jid in live:
            continue
        if _has_outcome(state_dir, jid):
            continue
        if _is_refused(state_dir, jid):
            continue
        out.append(jid)
    return sorted(set(out))


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
        candidates = orphan_jids(state_dir)
    except Exception as exc:                       # housekeeping must not fail a start
        log("orphan re-add skipped: %s: %s" % (type(exc).__name__, exc))
        return readded

    for jid in candidates:
        try:
            if _already_resubmitted_in_for_fleet(state_dir, jid):
                continue                           # still parked from a prior start; not twice
            text = _goal_text_for(state_dir, jid)
            if not text:
                log("orphan re-add: %s has no recoverable goal text; left for an operator" % jid)
                continue
            marker = {RESUBMIT_MARKER: True, "jid": jid}
            if park(jid, text, marker):
                readded.append(jid)
                log("orphan re-add: %s re-queued after restart (%s)" % (jid, RESUBMIT_MARKER))
            else:
                log("orphan re-add: %s could not be parked; left for an operator" % jid)
        except Exception as exc:
            log("orphan re-add: %s skipped: %s: %s" % (jid, type(exc).__name__, exc))

    _record(state_dir, readded)
    if readded:
        log("orphan re-add: %d goal(s) re-queued after restart" % len(readded))
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
