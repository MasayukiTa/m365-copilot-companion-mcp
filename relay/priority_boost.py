# -*- coding: utf-8 -*-
"""Priority boost for jobs that were ALREADY queued.

fleet_submit(priority=True) only helps at admission time: the goal jumps the pending queue
as it enters. A job that is already waiting/ready has no way to be moved to the front after
the fact, and there was no record of who moved it. This module is that missing half: a
read/write layer over two files under .fleet/tasks/ that marks a queued job as boosted and
keeps an append-only audit of every boost/unboost.

WHERE THE STATE LIVES, AND WHY HERE.

  * .fleet/tasks/boosts/<jid>.json -- one file per boosted job, the same one-file-per-id shape
    tools/fleet_intake.py already uses for refusals (.fleet/tasks/refused/<id>.json). Presence
    means boosted; unboost deletes the file. It lives under TASKS so a test that repoints
    TR.TASKS (conftest does this for every run) moves this with it.
  * .fleet/tasks/boost_audit.jsonl -- append-only, one JSON object per line, who/when/action
    for every boost and unboost. JSON Lines, utf-8, newline="\n", BOM-free on write and
    read-back with utf-8-sig, matching the repository's ledger rule.

SETTING. FLEET_PRIORITY_BOOST_ENABLED (default on). Set it to a false value (0/false/no/off)
to disable the whole feature: is_enabled() returns False, boost()/unboost() become no-ops
that return False, and is_boosted()/list_boosted() report nothing boosted. The cockpit reads
is_enabled() to hide the control.

NOTHING HERE RUNS A JOB. It records an intent. The scheduler reads is_boosted()/list_boosted()
to decide ordering; moving work is the consumer's job, exactly as admission is separate from
execution in fleet_intake.
"""
from __future__ import annotations

import io
import json
import os
import re
import time
import uuid

from relay import task_router as TR

#: Sub-directory under TR.TASKS holding one file per boosted job.
BOOSTS_DIR = "boosts"

#: Append-only audit log, beside the queue directories under TR.TASKS.
AUDIT_NAME = "boost_audit.jsonl"

#: Cap on free-text fields a human composes (actor, reason). The goal/jid are ids, not prose,
#: and are left whole; actor can name a person and an address, so it is redacted and capped the
#: same way fleet_intake treats `source`.
MAX_FIELD_CHARS = 300

#: An address in actor/reason is the agent naming a person. Redact at the writer, as
#: fleet_intake does, because several readers (cockpit, audit replay) share one writer.
_ADDRESS_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_TRUNCATED = "\u2026[cut]"


def _clean(text, limit: int = MAX_FIELD_CHARS) -> str:
    """Collapse whitespace, drop any address, cap -- and mark the cut. Mirrors fleet_intake._clean."""
    out = " ".join(_ADDRESS_RE.sub("<address redacted>", str(text or "")).split())
    if limit and len(out) > limit:
        cut = max(0, limit - len(_TRUNCATED))
        return out[:cut] + _TRUNCATED
    return out


def is_enabled() -> bool:
    """Whether priority boost is on. Default ON; an explicit false value turns the feature off.

    Read live (not cached at import) so a test or the owner can flip the env var between calls
    without reloading the module.
    """
    raw = os.environ.get("FLEET_PRIORITY_BOOST_ENABLED", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


def _tasks_dir(tasks_dir=None) -> str:
    return tasks_dir or TR.TASKS


def _boosts_dir(tasks_dir=None) -> str:
    return os.path.join(_tasks_dir(tasks_dir), BOOSTS_DIR)


def _boost_path(jid: str, tasks_dir=None) -> str:
    return os.path.join(_boosts_dir(tasks_dir), "%s.json" % jid)


def _audit_path(tasks_dir=None) -> str:
    return os.path.join(_tasks_dir(tasks_dir), AUDIT_NAME)


def _append_audit(action: str, jid: str, actor: str, reason: str = "", tasks_dir=None) -> None:
    """Append one audit line. JSON Lines, utf-8, newline="\n", no BOM. Never raises.

    A boost whose audit line could not be written still boosts; the marker file is the state and
    the audit is the account of it, so losing the account must not lose the state.
    """
    rec = {
        "ts": time.time(),
        "action": str(action or ""),
        "jid": str(jid or ""),
        "actor": _clean(actor or "agent"),
        "audit_id": uuid.uuid4().hex[:12],
    }
    if reason:
        rec["reason"] = _clean(reason)
    try:
        path = _audit_path(tasks_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def boost(jid: str, actor: str = "agent", reason: str = "", tasks_dir=None) -> bool:
    """Mark an already-queued job as boosted. Returns True on write, False if disabled/invalid.

    Idempotent: boosting an already-boosted job rewrites the marker (refreshing who/when) and
    still records the action, so the audit shows each press of the control.
    """
    if not is_enabled():
        return False
    jid = str(jid or "").strip()
    if not jid:
        return False
    rec = {
        "jid": jid,
        "boosted": True,
        "actor": _clean(actor or "agent"),
        "boosted_at": time.time(),
    }
    if reason:
        rec["reason"] = _clean(reason)
    try:
        path = _boost_path(jid, tasks_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        return False
    _append_audit("boost", jid, actor, reason, tasks_dir)
    return True


def unboost(jid: str, actor: str = "agent", reason: str = "", tasks_dir=None) -> bool:
    """Clear a job's boost. Returns True if a marker was removed, False otherwise.

    When the feature is disabled this is a no-op returning False -- there is nothing to clear
    because nothing can be boosted. Removing a marker that was not there returns False and does
    not write an audit line (there was no action to record).
    """
    if not is_enabled():
        return False
    jid = str(jid or "").strip()
    if not jid:
        return False
    path = _boost_path(jid, tasks_dir)
    try:
        existed = os.path.exists(path)
        if existed:
            os.remove(path)
    except Exception:
        return False
    if existed:
        _append_audit("unboost", jid, actor, reason, tasks_dir)
    return existed


def is_boosted(jid: str, tasks_dir=None) -> bool:
    """Whether a job currently carries a boost marker. False when the feature is disabled."""
    if not is_enabled():
        return False
    jid = str(jid or "").strip()
    if not jid:
        return False
    return os.path.exists(_boost_path(jid, tasks_dir))


def list_boosted(tasks_dir=None) -> list:
    """Return the jids of every currently-boosted job (sorted). Empty when disabled.

    Reads the boosts directory, not the audit: the audit is history, the directory is the
    present. A caller that wants the count for the cockpit's "N boosted" badge takes len() of
    this.
    """
    if not is_enabled():
        return []
    d = _boosts_dir(tasks_dir)
    try:
        names = os.listdir(d)
    except Exception:
        return []
    out = [n[:-5] for n in names if n.endswith(".json")]
    out.sort()
    return out


def boosted_count(tasks_dir=None) -> int:
    """How many jobs are boosted right now. 0 when disabled."""
    return len(list_boosted(tasks_dir))


def read_audit(tasks_dir=None, limit: int = 0) -> list:
    """Return audit records oldest-first. Read-back uses utf-8-sig so a BOM-prefixed line (if a
    non-Python writer ever touched the file) still parses. Unparseable lines are skipped rather
    than failing the whole read -- one broken line must not hide the rest of the account.

    limit>0 returns only the most recent `limit` records (still oldest-first within that slice).
    """
    path = _audit_path(tasks_dir)
    recs = []
    try:
        with io.open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    if limit and len(recs) > limit:
        recs = recs[-limit:]
    return recs
