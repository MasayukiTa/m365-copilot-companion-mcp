"""Resume bookkeeping that must not depend on a live coordinator.

Pure functions and small file helpers used by the coordinator (relay.fleet_runner,
relay.relay_fleet) and by the resumer (scripts/win/resume_interrupted_fleet.py). Nothing here
imports fleet_runner or relay_fleet, so both can import it.

Design: docs/private/20260930_fleet_interrupted_resume_design.md, sections 2-4.

* Campaign ledger reading (`.fleet/campaigns.jsonl`): which parents split, which children are
  unfinished, which merges finished.
* `resume_gate`: the loop guard for automatic resume (pure; mirrored in supervisor.ps1 by
  Get-FleetResumeGate / Test-FleetShouldAutoResume).
* `FreeSpaceRing` and `enable_fault_log`: crash forensics that keep working on a full disk
  because their files are pre-allocated and overwritten in place.

NO POLICY VALUE IS DEFINED HERE. The disk floor is only ever READ (the caller passes it in);
MAX_AUTO_RESUMES / BACKOFF_BASE_S are the loop-guard numbers the design proposes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

CAMPAIGNS_FILE = "campaigns.jsonl"
DONE_MAP_FILE = "last_run_done.json"

#: Design section 2, "Loop guard". Open question 3 in the design asks the owner to confirm.
MAX_AUTO_RESUMES = 3
BACKOFF_BASE_S = 300.0

GB = 1024.0 ** 3

#: Fixed strings in a coordinator log that mean "the disk was full", design section 4 (a).
ENOSPC_MARKERS = ("No space left", "WinError 112", "Errno 28",
                  "database or disk is full", "disk I/O error")


# --------------------------------------------------------------------------- keys

def text_key(text):
    """The text hash fleet_runner._goal_key uses. Duplicated (not imported) to keep this module
    import-safe from relay_fleet; tests/test_fleet_resume_keys pins that the two agree."""
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16]


def goal_resume_key(goal):
    """jid first, else the text hash -- identical to fleet_runner._goal_resume_key."""
    if isinstance(goal, dict):
        jid = str(goal.get("jid") or "").strip()
        if jid:
            return "jid:" + jid
        return text_key(goal.get("text") or goal.get("goal") or "")
    return text_key(str(goal or ""))


# --------------------------------------------------------------------------- campaigns

def read_done_map(state_dir):
    try:
        with open(os.path.join(state_dir, DONE_MAP_FILE), encoding="utf-8-sig") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def read_campaigns(state_dir):
    """{campaign_id: family} from campaigns.jsonl via fanout.campaigns_from_ledger; {} when
    the file is absent or unreadable. Families without a header are not returned."""
    path = os.path.join(state_dir, CAMPAIGNS_FILE)
    try:
        from relay import fanout
        with open(path, encoding="utf-8") as fh:
            return fanout.campaigns_from_ledger(fh) or {}
    except Exception:
        return {}


def campaign_id_for_goal(text):
    from relay import fanout
    return fanout.campaign_id_for(text)


def has_campaign_header(state_dir, text):
    """G1: did a campaign for this parent goal text get its header line on disk?"""
    return campaign_id_for_goal(text) in read_campaigns(state_dir)


def _child_done(child, fam, done_map):
    """A child counts as finished when its resume key is DONE in the done-map, or a
    `child_result` line records its slice as DONE (the fallback for degraded old lines whose
    stored text was truncated and therefore hashes differently)."""
    goal = child.get("goal")
    if isinstance(goal, dict):
        key = goal_resume_key(goal)
    else:
        key = text_key(child.get("text") or "")
    if done_map.get(key) == "DONE":
        return True
    idx = child.get("subtask_index")
    for r in fam.get("child_results") or []:
        if r.get("subtask_index") == idx and str(r.get("outcome") or "").upper() == "DONE":
            return True
    return False


#: Hard ceiling on what one resume may queue: max(RESUME_CAP_FLOOR, RESUME_CAP_FACTOR x the
#: interrupted run's own goal count). Membership scoping is the real fix; this is the net that
#: turns any future membership mistake into a refusal instead of hundreds of queued goals.
RESUME_CAP_FLOOR = 20
RESUME_CAP_FACTOR = 4
#: Bounds for the campaign plans embedded in an interrupted snapshot.
SNAPSHOT_MAX_CAMPAIGNS = 40
SNAPSHOT_MAX_CHILDREN = 64
RESUME_CAP_REASON = "resume_queue_cap_exceeded"


def resume_queue_cap(original_goal_count):
    try:
        n = int(original_goal_count or 0)
    except (TypeError, ValueError):
        n = 0
    return max(RESUME_CAP_FLOOR, RESUME_CAP_FACTOR * max(n, 0))


def run_identity(run_id, workers):
    """(run_ids, worker_campaign_ids, goal_texts) of one run, from its worker entries."""
    run_ids = {str(run_id)} if run_id else set()
    cids, texts = set(), []
    for w in workers or []:
        if not isinstance(w, dict):
            continue
        if w.get("run_id"):
            run_ids.add(str(w["run_id"]))
        c = w.get("campaign_id") or w.get("campaign")
        if c:
            cids.add(str(c))
        g = w.get("goal")
        if isinstance(g, dict):
            g = g.get("text") or g.get("goal")
        if isinstance(g, str) and g.strip():
            texts.append(g)
    return run_ids, cids, texts


def campaigns_of_run(fams, run_ids=(), worker_cids=(), goal_texts=(), prior_cids=()):
    """The campaign ids in `fams` that BELONG to one run. Fail closed: nothing is a member
    unless some evidence says so, so a ledger of old campaigns contributes none.

    * header stamped with a run id (new ledgers): member iff the stamp is one of `run_ids`;
    * header with no stamp (legacy ledgers carry no run id and no timestamp): member iff the
      campaign id is the hash of a goal this run held (the parent job), which is how a split
      parent is tied to its family;
    * either kind: member when a worker of this run carries the campaign id, or `prior_cids`
      (the scoped list of the run this one resumed) names it.
    """
    run_ids = {str(r) for r in run_ids or () if r}
    worker_cids = {str(c) for c in worker_cids or () if c}
    prior = {str(c) for c in prior_cids or () if c}
    parents = set()
    for t in goal_texts or ():
        try:
            parents.add(campaign_id_for_goal((t or "").strip()))
            parents.add(campaign_id_for_goal(t or ""))
        except Exception:
            pass
    out = set()
    for cid, fam in (fams or {}).items():
        stamped = [str(r) for r in (fam.get("run_ids") or []) if r]
        if cid in worker_cids or cid in prior:
            out.add(cid)
        elif stamped:
            if run_ids & set(stamped):
                out.add(cid)
        elif cid in parents:
            out.add(cid)
    return out


def _snapshot_files(state_dir):
    d = os.path.join(state_dir, "interrupted")
    try:
        return [os.path.join(d, n) for n in os.listdir(d) if n.endswith(".json")]
    except OSError:
        return []


def _scoped_ids(data):
    if not isinstance(data, dict) or not data.get("campaigns_scoped"):
        return set()
    return {str(c.get("campaign_id")) for c in data.get("campaigns") or []
            if isinstance(c, dict) and c.get("campaign_id")}


def lineage_campaign_ids(state_dir, lineage, depth=6):
    """Scoped campaign ids carried by the snapshot chain a resumed run descends from."""
    out, seen = set(), set()
    while lineage and depth > 0 and lineage not in seen and re.fullmatch(r"[A-Za-z0-9_\-]+", str(lineage)):
        seen.add(lineage)
        depth -= 1
        data = _snapshot_read(os.path.join(state_dir, "interrupted", str(lineage) + ".json"))
        if not isinstance(data, dict):
            break
        out |= _scoped_ids(data)
        lineage = (data.get("resume") or {}).get("lineage")
    return out


def interrupted_run_scope(state_dir, lineage=None):
    """The campaign ids that belong to the run being resumed -> (set, origin).

    Evidence, in order: the snapshot the resumer named (MCP_FLEET_RESUME_LINEAGE), else the
    newest pending/resumed snapshot; its workers give the run ids, the campaign ids and the
    parent goal texts; last_run_goals.json adds goal texts. No snapshot and no goals ledger
    -> empty set (nothing is resumed from the campaign ledger).
    """
    if lineage is None:
        lineage = os.environ.get("MCP_FLEET_RESUME_LINEAGE", "")
    lineage = str(lineage or "").strip()
    snap = None
    if lineage and re.fullmatch(r"[A-Za-z0-9_\-]+", lineage):
        snap = _snapshot_read(os.path.join(state_dir, "interrupted", lineage + ".json"))
    if not isinstance(snap, dict):
        snap, best = None, -1.0
        for path in _snapshot_files(state_dir):
            d = _snapshot_read(path)
            if isinstance(d, dict) and d.get("state") in ("pending", "resumed"):
                ts = _num(d.get("written_ts"), 0.0)
                if ts > best:
                    snap, best = d, ts
    run_ids, cids, texts, prior = set(), set(), [], set()
    origin = "none"
    if isinstance(snap, dict):
        origin = "snapshot"
        run_ids, cids, texts = run_identity(snap.get("run_id"), snap.get("workers"))
        prior |= _scoped_ids(snap)
        prior |= lineage_campaign_ids(state_dir, (snap.get("resume") or {}).get("lineage"))
    try:
        with open(os.path.join(state_dir, "last_run_goals.json"), encoding="utf-8-sig") as fh:
            led = json.load(fh)
        for e in led.get("goals") or []:
            if isinstance(e, dict) and e.get("text"):
                texts.append(str(e["text"]))
        if origin == "none" and texts:
            origin = "goals_ledger"
    except Exception:
        pass
    if not (run_ids or cids or texts or prior):
        return set(), origin
    return campaigns_of_run(read_campaigns(state_dir), run_ids, cids, texts, prior), origin


def resume_children_goals(state_dir, done_map=None, log=print, scope=None):
    """G2: goals to re-queue for campaign children that are not DONE.

    Only campaigns that belong to the interrupted run (`scope`, a set of campaign ids; derived
    by interrupted_run_scope when None) with a header and WITHOUT `merge_done` count. The
    ledger keeps every campaign ever split, so taking all unfinished ones re-queued 545 goals
    for a 2-goal run. Returns (goals, degraded) where `degraded` is the number of goals
    rebuilt from the old truncated `text` + header cwd because the child line predates the
    `goal` object.
    """
    done_map = read_done_map(state_dir) if done_map is None else done_map
    if scope is None:
        scope, _origin = interrupted_run_scope(state_dir)
    scope = set(scope)
    goals, degraded, skipped = [], 0, 0
    camps = read_campaigns(state_dir)
    for cid, fam in sorted(camps.items()):
        if cid not in scope:
            skipped += 1
            continue
        if fam.get("merge_done"):
            continue
        for child in fam.get("children") or []:
            if _child_done(child, fam, done_map):
                continue
            g = child.get("goal")
            if isinstance(g, dict) and (g.get("text") or g.get("goal")):
                goal = dict(g)
            else:
                text = child.get("text") or ""
                if not text:
                    continue
                goal = {"text": text, "campaign_id": cid,
                        "task_id": child.get("task_id"), "role": "subtask",
                        "subtask_index": child.get("subtask_index"),
                        "subtask_of": fam.get("n") or None, "depth": 1}
                if fam.get("cwd"):
                    goal["cwd"] = fam.get("cwd")
                goal["degraded"] = True
                degraded += 1
                log("[resume] child %s of campaign %s rebuilt from truncated text "
                    "(degraded: old ledger line has no goal object)"
                    % (child.get("subtask_index"), cid))
            goals.append(goal)
    log("[resume] campaign ledger: %d campaign(s) belong to the interrupted run, %d other(s) "
        "ignored" % (len(scope & set(camps)), skipped))
    return goals, degraded


def rehydrate_decision(fam, done_map):
    """G3: what to do with a campaign family found on disk at coordinator start.

    Returns one of "drop" (finished), "carry" (unmerged: normal path) or "reissue"
    (merge was queued but never finished and no aggregator DONE is on record; allowed once).
    """
    if fam.get("merge_done"):
        return "drop"
    if not fam.get("merged"):
        return "carry"
    if not fam.get("agg_key"):
        # A `merged` line from before merge_done existed (it has no agg_key): its meaning was
        # "assembled", and a ledger full of historical campaigns must not be merged again.
        return "drop"
    if fam.get("agg_key") and done_map.get(fam.get("agg_key")) == "DONE":
        return "drop"                      # finished after all; merge_done just was not written
    if int(fam.get("merge_requeued") or 0) >= 1:
        return "drop"                      # automatic cap 1: a person decides after this
    return "reissue"


# --------------------------------------------------------------------------- resume gate

def crash_signature(evidence_lines=(), exception_code="", module=""):
    """(signature, enospc). sha1 of exception code | faulting module | error class, where the
    error class is the first ENOSPC marker found in the evidence text (else "")."""
    text = "\n".join(str(l) for l in (evidence_lines or ()))
    klass = ""
    for m in ENOSPC_MARKERS:
        if m in text:
            klass = m
            break
    raw = "%s|%s|%s" % (exception_code or "", module or "", klass)
    sig = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16] if raw != "||" else ""
    return sig, bool(klass)


def _num(v, default=None):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def resume_gate(record, now, free_bytes, floor_gb, signature, coordinator_live=False,
                enospc=False):
    """PURE. May this interrupted run be resumed AUTOMATICALLY now? -> (ok, reason).

    record   -- the snapshot dict (or a synthetic {"state": "pending"}); read with .get
    now      -- epoch seconds, injected
    free_bytes -- free bytes on the .fleet drive now (None = unknown)
    floor_gb -- the operator's disk floor as READ from the existing setting; None/<=0 = none
    signature -- crash signature of this death ("" = unknown)

    Checked in order, first refusal wins. The reason strings are mirrored verbatim by
    supervisor.ps1 Get-FleetResumeGate and compared by scripts/test_supervisor_fleet_resume_guard.py.
    """
    record = record if isinstance(record, dict) else {}
    if record.get("stop_requested") or (record.get("interrupted") or {}).get("stop_requested"):
        return False, "stop_requested"
    if coordinator_live:
        return False, "coordinator_live"
    state = record.get("state") or "pending"
    if state != "pending":
        return False, "state_%s" % state
    resume = record.get("resume") or {}
    count = int(_num(resume.get("count"), 0) or 0)
    if count >= MAX_AUTO_RESUMES:
        return False, "max_resumes"
    last_ts = _num(resume.get("last_ts"))
    if count > 0 and last_ts is not None:
        wait = BACKOFF_BASE_S * (2 ** count)
        if now < last_ts + wait:
            return False, "backoff"
    free = _num(free_bytes)
    floor = _num(floor_gb)
    if floor is not None and floor > 0 and free is not None and free < floor * GB:
        return False, "below_floor"
    prev_free = _num(resume.get("last_free_bytes"))
    if prev_free is None:
        prev_free = _num((record.get("interrupted") or {}).get("free_bytes_at_detection"))
        if prev_free is None:
            prev_free = _num((record.get("interrupted") or {}).get("free_bytes_at_death"))
    no_gain = free is None or prev_free is None or free <= prev_free
    if signature and signature == resume.get("last_signature") and no_gain:
        return False, "same_crash_no_more_space"
    if enospc and (free is None or prev_free is None or free <= prev_free):
        return False, "disk_full_no_more_space"
    return True, "ok"


def _snapshot_write(path, data):
    from relay.fleet_reaper import _write_atomic
    _write_atomic(path, data)


def _snapshot_read(path):
    from relay.fleet_reaper import _read_json
    return _read_json(path)


def record_resume_refused(state_dir, lineage, now, queued, cap):
    """The coordinator refused to queue `queued` goals (> cap): the snapshot stays `pending`
    and carries the reason. The count is NOT reset, so the loop guard still ends retries."""
    try:
        if not lineage or not re.fullmatch(r"[A-Za-z0-9_\-]+", str(lineage)):
            return False
        path = os.path.join(state_dir, "interrupted", str(lineage) + ".json")
        data = _snapshot_read(path)
        if not isinstance(data, dict):
            return False
        data.setdefault("resume", {})["blocked"] = {
            "reason": RESUME_CAP_REASON, "ts": now, "queued": queued, "cap": cap}
        data["state"] = "pending"
        data["state_ts"] = now
        _snapshot_write(path, data)
        return True
    except Exception:
        return False


def record_resume(path, now, free_bytes, signature):
    """After a real relaunch: state=resumed, resume.count += 1, history. Never raises."""
    try:
        data = _snapshot_read(path)
        if not isinstance(data, dict):
            return False
        r = data.setdefault("resume", {})
        r["count"] = int(_num(r.get("count"), 0) or 0) + 1
        r["last_ts"] = now
        r["last_signature"] = signature or ""
        r["last_free_bytes"] = free_bytes
        r.setdefault("history", []).append(
            {"ts": now, "free_bytes": free_bytes, "signature": signature or ""})
        refused = (r.get("blocked") or {}).get("reason") == RESUME_CAP_REASON
        if not refused:
            r.pop("blocked", None)
            data["state"] = "resumed"
            data["state_ts"] = now
        _snapshot_write(path, data)
        return True
    except Exception:
        return False


def record_blocked(path, now, reason, free_bytes, signature):
    """A refused resume: state unchanged, resume.blocked = {...}; the cap flips it to gave_up."""
    try:
        data = _snapshot_read(path)
        if not isinstance(data, dict):
            return False
        data.setdefault("resume", {})["blocked"] = {
            "reason": reason, "ts": now, "free_bytes": free_bytes, "signature": signature or ""}
        if reason == "max_resumes":
            data["state"] = "gave_up"
            data["state_ts"] = now
        _snapshot_write(path, data)
        return True
    except Exception:
        return False


def inherit_resume(fleet_dir, lineage):
    """The `resume` block of the snapshot a resumed run descends from, so a run that dies
    again keeps its count. `lineage` is the parent run_id the resumer put in the environment
    (MCP_FLEET_RESUME_LINEAGE) and the coordinator copied into its marker."""
    try:
        if not lineage or not re.fullmatch(r"[A-Za-z0-9_\-]+", str(lineage)):
            return None
        data = _snapshot_read(os.path.join(fleet_dir, "interrupted", str(lineage) + ".json"))
        if isinstance(data, dict) and isinstance(data.get("resume"), dict):
            out = json.loads(json.dumps(data["resume"]))
            out.pop("blocked", None)
            out["lineage"] = str(lineage)
            return out
    except Exception:
        pass
    return None


def tail_lines(path, n=200):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            return fh.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return []


def newest_coordinator_log(state_dir, pid=None):
    """Path of the newest coordinator_*[_p<pid>].log, or None."""
    try:
        names = [n for n in os.listdir(state_dir)
                 if n.startswith("coordinator_") and n.endswith(".log")
                 and (pid is None or ("_p%s." % pid) in n)]
        if not names:
            return None
        return max((os.path.join(state_dir, n) for n in names), key=os.path.getmtime)
    except OSError:
        return None


# --------------------------------------------------------------------------- free-space ring

RING_SLOTS = 256
RING_SLOT_BYTES = 128
RING_FILE = "free_space_ring.jsonl"
RING_SAMPLE_S = 30.0


class FreeSpaceRing:
    """Fixed-size ring of free-space samples: RING_SLOTS x RING_SLOT_BYTES, pre-allocated.

    Overwrites in place, so the file never grows and a full disk cannot make a write fatal (no
    new cluster is allocated). Every failure is swallowed and counted in `failures`. It only
    REPORTS: no threshold is defined here.
    """

    def __init__(self, path, slots=RING_SLOTS, slot_bytes=RING_SLOT_BYTES):
        self.path = path
        self.slots = slots
        self.slot_bytes = slot_bytes
        self.failures = 0
        self.last_sample_ts = 0.0
        self._n = 0
        self._fh = None
        try:
            self._open()
        except Exception:
            self.failures += 1
            self._fh = None

    def _open(self):
        size = self.slots * self.slot_bytes
        exists = os.path.isfile(self.path) and os.path.getsize(self.path) == size
        if not exists:
            with open(self.path, "wb") as fh:
                fh.write(b"\0" * size)
        self._fh = open(self.path, "r+b", buffering=0)
        if exists:
            rows = read_ring(self.path, self.slots, self.slot_bytes, with_slot=True)
            if rows:
                last = max(rows, key=lambda r: r[1].get("ts", 0))
                self._n = last[0] + 1

    def sample(self, free_bytes, total_bytes, floor_gb, pid=None, now=None):
        """Overwrite the next slot. Returns True when written."""
        now = time.time() if now is None else now
        if self._fh is None:
            self.failures += 1
            return False
        try:
            rec = {"ts": round(now, 1), "pid": pid if pid is not None else os.getpid(),
                   "free_bytes": free_bytes, "total_bytes": total_bytes,
                   "floor_gb": floor_gb}
            line = json.dumps(rec, separators=(",", ":")).encode("ascii")
            if len(line) > self.slot_bytes - 1:
                line = line[: self.slot_bytes - 1]
            line = line.ljust(self.slot_bytes - 1, b" ") + b"\n"
            self._fh.seek((self._n % self.slots) * self.slot_bytes)
            self._fh.write(line)
            self._n += 1
            self.last_sample_ts = now
            return True
        except Exception:
            self.failures += 1
            return False

    def close(self):
        try:
            if self._fh:
                self._fh.close()
        except Exception:
            pass
        self._fh = None


def read_ring(path, slots=RING_SLOTS, slot_bytes=RING_SLOT_BYTES, with_slot=False):
    """Samples sorted by ts (oldest first). Blank/torn slots are skipped."""
    out = []
    try:
        with open(path, "rb") as fh:
            blob = fh.read(slots * slot_bytes)
    except OSError:
        return out
    for i in range(0, len(blob) - slot_bytes + 1, slot_bytes):
        raw = blob[i:i + slot_bytes].replace(b"\0", b"").strip()
        if not raw.startswith(b"{"):
            continue
        try:
            rec = json.loads(raw.decode("ascii", "replace"))
        except Exception:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("ts"), (int, float)):
            out.append((i // slot_bytes, rec) if with_slot else rec)
    out.sort(key=(lambda r: r[1]["ts"]) if with_slot else (lambda r: r["ts"]))
    return out


def disk_status(free_bytes, total_bytes, floor_gb, sampled_ts, log_failures):
    """status.json `disk` block. `below_floor` is None when the floor is unset/0, else the
    plain comparison free_gb < floor_gb -- no threshold of its own."""
    floor = _num(floor_gb)
    below = None
    if floor is not None and floor > 0 and free_bytes is not None:
        below = (free_bytes / GB) < floor
    return {"free_bytes": free_bytes, "total_bytes": total_bytes,
            "floor_gb": floor if (floor is not None and floor > 0) else None,
            "below_floor": below, "sampled_ts": sampled_ts, "log_failures": log_failures}


# --------------------------------------------------------------------------- fault log

FAULT_LOG_BYTES = 65536
_fault_fh = None


def enable_fault_log(state_dir, pid=None):
    """faulthandler.enable() to `.fleet/fault_p<pid>.log`, a 64 KB file zero-filled up front
    and kept open, so a native crash leaves a trace even with no free space. Returns the path,
    or None when it could not be set up. Never raises.

    The name is deliberately not `coordinator_*.log` (retention keeps only the newest 20 of
    those) and not `*.log.N`.
    """
    global _fault_fh
    try:
        import faulthandler
        pid = os.getpid() if pid is None else pid
        path = os.path.join(state_dir, "fault_p%s.log" % pid)
        with open(path, "wb") as fh:
            fh.write(b"\0" * FAULT_LOG_BYTES)
        _fault_fh = open(path, "r+b", buffering=0)
        faulthandler.enable(file=_fault_fh, all_threads=True)
        return path
    except Exception:
        return None
