"""Durable per-job tombstones: a job a human stopped stays stopped across restarts.

WHY THIS EXISTS. PR 409 (fix(resume): do not re-queue user-cancelled jobs) recorded a
CANCELLED outcome in the resume done-map (last_run_done.json) and taught _resume_goals to
skip it. That closed ONE path (the goals-ledger resume) but left others open, and it depended
on a write that only happens when a worker reaches its own terminal hook:

  * A worker the operator `close`d can die before _update_done_map records CANCELLED, so the
    done-map never learns the job was stopped (jobs 361689fccd96, 01fdaeb8b066, 506c2633931b,
    a9e2d2031457 have NO outcome file under tasks/done at all, yet still ran after a restart).
  * The done-map is keyed by goal text / jid of the LEDGER goal; fan-out children carry their
    own jids and are resumed through campaigns.jsonl (fleet_resume._child_done), not the
    goals-ledger, so a child of a cancelled parent is not covered by the parent's done entry.
  * A pending add_goal command (relay/task_router.add_goal_to_live_fleet ->
    .fleet/commands.d) adopted by a fresh runner (fleet_runner.claim_adopt_command) is a NEW
    run that never consults the done-map at all.
  * A job parked in tasks/for_fleet or tasks/pending is re-admitted on the next intake with no
    done-map check.

THE ROOT RULE. The moment a job is cancelled OR any of its workers is closed by the operator,
write a durable tombstone keyed by jid: .fleet/tasks/closed/<jid>.json. EVERY path that would
start or resume a worker checks is_tombstoned(state_dir, jid) first and refuses. Fan-out
children inherit the tombstone of any tombstoned ancestor (child_of / parent_jid). A tombstone
is removed ONLY by an explicit operator re-submission that carries a NEW jid -- never by a
restart, a resume, or a reaper. The done-map remains as the fast in-run skip; the tombstone is
the durable floor that does not depend on a terminal hook having run.

NO POLICY beyond "stopped stays stopped": this module only reads and writes the tombstone
files. The decision of WHEN to write one lives at the cancel / close call sites.
"""

import json
import os
import time

#: Directory under the fleet state dir that holds one JSON per tombstoned jid.
TOMBSTONE_SUBDIR = ("tasks", "closed")

#: Reasons, named so a cockpit refusal and a test can speak the same words.
REASON_CANCELLED = "cancelled"              # the job's outcome is cancelled
REASON_OPERATOR_CLOSE = "operator_close"    # a `close` command / 手動で停止・タブ解放
REASON_INHERITED = "inherited"              # a fan-out child of a tombstoned ancestor


def _safe_jid(jid) -> str:
    """A jid usable as a file name: non-empty, no path separators, bounded. Empty on reject.

    A tombstone is a safety floor; a malformed jid must never let it write outside tasks/closed
    or collapse two jids into one file. Mirrors the _is_safe_id screen the command channel uses.
    """
    s = str(jid or "").strip()
    if not s or len(s) > 128:
        return ""
    for ch in s:
        if not (ch.isalnum() or ch in "-_."):
            return ""
    if s in (".", ".."):
        return ""
    return s


def tombstone_dir(state_dir) -> str:
    return os.path.join(state_dir, *TOMBSTONE_SUBDIR)


def tombstone_path(state_dir, jid) -> str:
    """Absolute path of the tombstone for jid, or "" if jid is unusable."""
    safe = _safe_jid(jid)
    if not safe:
        return ""
    return os.path.join(tombstone_dir(state_dir), safe + ".json")


def _write_atomic(path, payload) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def write_tombstone(state_dir, jid, reason=REASON_CANCELLED, *, worker=None,
                    goal_text=None, parent_jid=None, extra=None) -> bool:
    """Record that jid must never start again (until an operator re-submits with a NEW jid).

    Idempotent: re-closing an already-tombstoned job keeps the FIRST reason/timestamp and only
    appends the later event to `events`, so the audit trail shows cancelled-then-closed without
    losing which came first. Best-effort and never raises -- a tombstone write that fails must
    not take down the cancel path, but the caller can read the bool to log a warning.
    """
    path = tombstone_path(state_dir, jid)
    if not path:
        return False
    now = time.time()
    event = {"reason": reason, "ts": now}
    if worker is not None:
        event["worker"] = str(worker)
    try:
        existing = _read_one(path)
    except Exception:
        existing = None
    try:
        if isinstance(existing, dict) and existing.get("jid"):
            events = list(existing.get("events") or [])
            events.append(event)
            existing["events"] = events
            existing["updated_ts"] = now
            _write_atomic(path, existing)
            return True
        payload = {
            "jid": _safe_jid(jid),
            "reason": reason,
            "ts": now,
            "updated_ts": now,
            "events": [event],
        }
        if goal_text is not None:
            payload["goal_text"] = str(goal_text)[:2000]
        if parent_jid is not None:
            payload["parent_jid"] = _safe_jid(parent_jid) or None
        if isinstance(extra, dict):
            for k, v in extra.items():
                payload.setdefault(str(k), v)
        _write_atomic(path, payload)
        return True
    except Exception:
        return False


def _read_one(path):
    with open(path, encoding="utf-8-sig") as fh:
        d = json.load(fh)
    return d if isinstance(d, dict) else None


def is_tombstoned(state_dir, jid) -> bool:
    """Does a durable tombstone exist for jid? Missing dir / unusable jid -> False.

    This is the one predicate every start/resume/re-admission path calls. It is a pure disk
    read (no coordinator, no done-map) so it holds across a restart the instant the file exists.
    """
    path = tombstone_path(state_dir, jid)
    if not path:
        return False
    return os.path.isfile(path)


def tombstone_reason(state_dir, jid):
    """The recorded reason for jid's tombstone, or None if there is none / it is unreadable."""
    path = tombstone_path(state_dir, jid)
    if not path or not os.path.isfile(path):
        return None
    try:
        d = _read_one(path)
    except Exception:
        return None
    if isinstance(d, dict):
        return d.get("reason")
    return None


def tombstoned_jids(state_dir) -> set:
    """Every tombstoned jid on disk. Missing dir -> empty set. Never raises."""
    out = set()
    d = tombstone_dir(state_dir)
    try:
        names = os.listdir(d)
    except Exception:
        return out
    for name in names:
        if name.endswith(".json") and not name.endswith(".tmp"):
            out.add(name[:-5])
    return out


def goal_jid(goal):
    """The jid of a goal dict (or object with a .jid), as the tombstone key; "" when absent.

    Accepts the shapes the pipeline actually carries: a goal dict ({"jid": ...}), a worker-like
    object (getattr .jid), or a ledger entry. Text-only / legacy goals have no jid and so can
    never be tombstoned by this key -- which is correct: the tombstone is a jid rule, and the
    done-map still covers the legacy text path.
    """
    if isinstance(goal, dict):
        return _safe_jid(goal.get("jid"))
    jid = getattr(goal, "jid", None)
    return _safe_jid(jid)


#: The fields a fan-out child actually carries that name an ancestor whose tombstone it must
#: inherit. fanout.child_goals writes parent_task_id / campaign_id / task_id / root_id (NOT
#: parent_jid): a child has NO jid of its own, so inheritance has to run through THESE ids, the
#: same ones family_view joins a family by. The older *_jid names are kept too so a caller that
#: does carry an explicit parent jid still works. Order: nearest ancestor first.
_ANCESTOR_FIELDS = (
    "parent_jid", "child_of", "family_jid", "campaign_jid", "root_jid",
    "parent_task_id", "task_id", "campaign_id", "root_id",
)


def _ancestor_jids(goal):
    """Candidate ancestor ids a child goal carries, for inheritance. Order: nearest first.

    A fan-out child is keyed by parent_task_id / campaign_id / root_id (see fanout.child_goals
    and relay/family_view.py), never by a parent_jid field, so those are the ids that let a
    child inherit a stopped ancestor's tombstone. The close/cancel site tombstones the worker's
    OWN task_id / parent_task_id / campaign_id / root_id (see worker_identities) so these keys
    have something to match.
    """
    out = []
    seen = set()
    get = goal.get if isinstance(goal, dict) else (lambda k: getattr(goal, k, None))
    for k in _ANCESTOR_FIELDS:
        v = _safe_jid(get(k))
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def worker_identities(worker):
    """Every id a closed/cancelled worker should be tombstoned under: its jid AND the fan-out
    identities (task_id / parent_task_id / campaign_id / root_id) its children inherit through.

    Tombstoning ALL of them -- not only the jid -- is what lets a cancelled fan-out PARENT stop
    its children: the children carry no jid, but they do carry the parent's task_id/campaign/
    root, and blocked_jid checks those via _ancestor_jids. Returns a de-duplicated list; the
    worker's own jid, when present, is first so a plain (non-fan-out) worker is unaffected.
    """
    out = []
    seen = set()
    get = worker.get if isinstance(worker, dict) else (lambda k: getattr(worker, k, None))
    env = None if isinstance(worker, dict) else getattr(worker, "task_envelope", None)
    def _add(v):
        s = _safe_jid(v)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    _add(get("jid"))
    for k in ("task_id", "parent_task_id", "campaign_id", "root_id"):
        _add(get(k))
        if env is not None:
            _add(getattr(env, k, None))
    return out


def blocked_jid(state_dir, goal):
    """If this goal must not start, return the tombstoned jid that blocks it; else None.

    Checks the goal's OWN jid first, then any ancestor jid it carries (a fan-out child inherits
    its parent's / family's tombstone). Returning the blocking jid -- not just True -- lets the
    caller name it in the cockpit refusal and write an `inherited` tombstone for the child.
    """
    own = goal_jid(goal)
    if own and is_tombstoned(state_dir, own):
        return own
    for anc in _ancestor_jids(goal):
        if is_tombstoned(state_dir, anc):
            return anc
    return None


def goal_or_worker_ids(obj):
    """Every id this goal/worker carries that could tie it to a stopped ancestor.

    The union of its OWN identities (jid + fan-out task_id/parent_task_id/campaign_id/
    root_id, via worker_identities) and the ancestor ids it inherits through
    (_ancestor_jids). Used to match a still-running descendant against the identities of a
    worker the operator just closed, WITHOUT touching disk -- an in-memory sibling of
    blocked_jid for the live-cancel path. De-duplicated, order not significant.
    """
    out = []
    seen = set()
    for v in list(worker_identities(obj)) + list(_ancestor_jids(obj)):
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def descendants_to_cancel(closed_identities, candidate_workers, closed_workers=()):
    """The still-running workers that inherit a CLOSED worker's identities, so closing a
    parent also stops children ALREADY RUNNING (not just future starts).

    THE GAP THIS CLOSES. The close site already tombstones every identity a closed worker
    carries (worker_identities) so a fan-out child is refused at the next start/resume/
    adopt door (blocked_jid). But a child that is ALREADY in flight has passed that door:
    nothing re-checks the tombstone mid-run, so it keeps going -- which is exactly how w33
    (a fan-out child carrying a closed parent's task_id/campaign_id/root_id, with no jid of
    its own) ran on for ~46 minutes and launched `cargo test` after its parent was closed.
    The tombstone is the durable floor; this is the live counterpart that cancels the
    descendant now.

    Pure and disk-free: it matches the closed identities against each candidate's own +
    inherited ids (goal_or_worker_ids) in memory, so it is independent of whether the
    tombstone write succeeded and is trivially testable.

    `closed_identities` is the union of worker_identities(w) over every worker NAMED in the
    close command; `candidate_workers` are the live workers; `closed_workers` are the
    worker objects that were named in the close directly. The named workers are excluded by
    IDENTITY -- not by trying to tell "own vs inherited" ids apart, because the fan-out
    fields overlap: a PARENT's own task_id is, by name, an "ancestor field" too
    (_ANCESTOR_FIELDS includes task_id, since that is the id a CHILD inherits through). So
    a parent would wrongly look like its own descendant. Excluding the named worker objects
    is unambiguous and leaves exactly the genuine descendants. Returns the de-duplicated
    list of candidates to cancel, input order preserved.
    """
    closed = {str(i) for i in (closed_identities or []) if i}
    if not closed:
        return []
    excluded = set()
    for cw in closed_workers or []:
        excluded.add(id(cw))
    out = []
    seen_names = set()
    for w in candidate_workers or []:
        if id(w) in excluded:
            continue
        ids = set(goal_or_worker_ids(w))
        if not ids or ids.isdisjoint(closed):
            continue
        name = None
        try:
            name = w.get("name") if isinstance(w, dict) else getattr(w, "name", None)
        except Exception:
            name = None
        key = name if name is not None else id(w)
        if key in seen_names:
            continue
        seen_names.add(key)
        out.append(w)
    return out


def remove_tombstone(state_dir, jid, *, operator_resubmit_jid=None) -> bool:
    """Remove a tombstone -- ONLY an explicit operator re-submission with a NEW jid may do this.

    The guard is the whole point: a restart / resume / reaper must NEVER call this without a
    distinct new jid, or the resurrection returns. We refuse to clear a tombstone when the
    re-submission jid is missing or equals the tombstoned jid (that would be the same request
    coming back, not a new decision). Returns True only when a file was actually removed.
    """
    safe = _safe_jid(jid)
    newj = _safe_jid(operator_resubmit_jid)
    if not safe or not newj or newj == safe:
        return False
    path = tombstone_path(state_dir, safe)
    if not path or not os.path.isfile(path):
        return False
    try:
        os.remove(path)
        return True
    except Exception:
        return False
