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


#: A worker's OWN identity -- the ids that name THIS worker, not the group it belongs to.
#: parent_task_id/campaign_id/root_id are GROUP ids a whole fan-out tree shares, so they are
#: deliberately NOT here: they belong to the root, not to every member.
_OWN_IDENTITY_FIELDS = ("jid", "task_id")

#: The group-root ids. A worker owns one of these ONLY when it is the root of that tree,
#: i.e. the value equals the worker's own jid/task_id. Otherwise the value merely says which
#: tree the worker is in -- shared by every sibling and the parent alike.
_GROUP_ROOT_FIELDS = ("campaign_id", "root_id")


def _get(obj, k):
    if isinstance(obj, dict):
        return obj.get(k)
    return getattr(obj, k, None)


def closing_anchor_ids(worker):
    """The ids that make a live worker a TRUE DESCENDANT of this closed worker.

    An anchor is an id such that any live worker whose ANCESTOR CHAIN contains it is a genuine
    descendant of `worker`. That is exactly `worker`'s OWN identity: its jid and its task_id --
    the ids a child names as its parent_task_id / inherited jid. A group-root id (campaign_id /
    root_id) is an anchor ONLY when `worker` is itself the root of that tree, i.e. the group id
    equals `worker`'s own jid/task_id; otherwise the group id is shared by `worker`'s siblings
    and parent too, and anchoring on it would cancel the whole tree -- the very over-cancel this
    fixes. Returns a set of safe-id strings.
    """
    own = set()
    env = None if isinstance(worker, dict) else getattr(worker, "task_envelope", None)
    for k in _OWN_IDENTITY_FIELDS:
        v = _safe_jid(_get(worker, k))
        if v:
            own.add(v)
        if env is not None:
            ev = _safe_jid(getattr(env, k, None))
            if ev:
                own.add(ev)
    anchors = set(own)
    for k in _GROUP_ROOT_FIELDS:
        v = _safe_jid(_get(worker, k))
        if v and v in own:          # worker IS the root of this tree -> the root id is an anchor
            anchors.add(v)
        if env is not None:
            ev = _safe_jid(getattr(env, k, None))
            if ev and ev in own:
                anchors.add(ev)
    return anchors


def _inherited_ancestor_ids(worker):
    """The ids by which `worker` points at an ANCESTOR -- its inherited chain, NOT its own name.

    This is _ancestor_jids minus the worker's own identity (task_id / jid): a child names its
    parent through parent_task_id and the inherited campaign_id/root_id, and through the older
    *_jid fields. We drop the worker's OWN task_id/jid so a worker is never its own ancestor.
    """
    own = set()
    for k in _OWN_IDENTITY_FIELDS:
        v = _safe_jid(_get(worker, k))
        if v:
            own.add(v)
    return [a for a in _ancestor_jids(worker) if a not in own]


def descendants_to_cancel(closed_identities, candidate_workers, closed_workers=()):
    """The still-running workers that are TRUE DESCENDANTS of a closed worker, so closing a
    parent also stops children ALREADY RUNNING (not just future starts) -- WITHOUT cancelling
    the closed worker's siblings or its parent.

    THE GAP THIS CLOSES. The close site tombstones every identity a closed worker carries so a
    fan-out child is refused at the next start/resume/adopt door (blocked_jid). But a child
    ALREADY in flight has passed that door and nothing re-checks the tombstone mid-run -- which
    is how w33 (a fan-out child carrying a closed parent's task_id/campaign_id/root_id, no jid
    of its own) ran on ~46 min and launched `cargo test` after its parent was closed.

    THE OVER-CANCEL THIS AVOIDS. Descendant-hood is NOT "shares any id". Siblings and the parent
    of a closed child carry the SAME campaign_id/root_id, so matching on a shared group id would
    cancel the whole fan-out tree from closing ONE child. A worker is a descendant of closed X
    only when its ANCESTOR CHAIN (parent_task_id, then that parent's parent, ..., or an inherited
    jid) reaches X's OWN task_id/jid. A shared campaign_id/root_id selects a worker ONLY when X
    is itself the root of that tree (X's own task_id/jid equals that root_id/campaign_id) -- see
    closing_anchor_ids.

    Multi-level: the chain is walked iteratively. Starting from the anchors of the closed
    workers, any candidate whose inherited ancestor ids reach the current frontier is a
    descendant, and ITS own task_id/jid joins the frontier so a grandchild two levels down is
    still caught even though it names only its immediate parent.

    Pure and disk-free. `closed_identities` is kept for call-site compatibility but anchoring is
    derived from `closed_workers` (the actual worker objects named in the close), because only a
    worker object can tell its OWN ids from the GROUP ids it merely shares. When `closed_workers`
    is empty, nothing is anchored (we will not guess descendant-hood from a flat id union, which
    is what caused the over-cancel). Named workers are excluded from the result by object
    identity. Returns the de-duplicated candidates to cancel, input order preserved.
    """
    excluded = {id(cw) for cw in (closed_workers or [])}
    frontier = set()
    for cw in closed_workers or []:
        frontier |= closing_anchor_ids(cw)
    if not frontier:
        return []
    cands = [w for w in (candidate_workers or []) if id(w) not in excluded]
    out = []
    seen_keys = set()
    # Iterate to a fixed point so multi-level descendants (grandchild, great-grandchild) are
    # reached: each pass may add new frontier ids (a caught worker's OWN identity), which can
    # in turn catch workers one level deeper on the next pass.
    changed = True
    while changed:
        changed = False
        for w in cands:
            anc = set(_inherited_ancestor_ids(w))
            if not anc or anc.isdisjoint(frontier):
                continue
            name = None
            try:
                name = w.get("name") if isinstance(w, dict) else getattr(w, "name", None)
            except Exception:
                name = None
            key = name if name is not None else id(w)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(w)
            # this worker's OWN identity now anchors ITS children on a later pass
            own_ids = {_safe_jid(_get(w, k)) for k in _OWN_IDENTITY_FIELDS}
            own_ids = {i for i in own_ids if i}
            if own_ids - frontier:
                frontier |= own_ids
                changed = True
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
