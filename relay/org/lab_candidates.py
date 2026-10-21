# -*- coding: utf-8 -*-
"""Count caps and a sweep plan for Evolution Lab candidate branches and worktrees (slice F2).

WHAT DECISION THIS SERVES. How many lab candidates may exist at once, and which ones a human may
remove. Branch and worktree sprawl in this repo is a measured problem: the 2026-10-04 cleanup found
51 stale branches out of 71. A lab that creates one branch and one worktree per candidate would
repeat that unless the count is capped and finished candidates are swept. The second reason is
safety: the production tree executes uncommitted edits live, so a candidate must never live inside
it. This module checks that every candidate worktree sits under the lab root (the layout rules of
relay/org/lab_isolation.py decide what a lab root may be; this module reuses its path helpers so
both use one definition of "inside", realpath-resolved and case-insensitive on Windows).

WHAT IT DOES. plan_sweep() takes candidate records and a policy and returns a deterministic plan.
It NEVER deletes anything and never runs git: it reads no file, writes no file, starts no process
and has no clock (the caller passes `now`). A human or a later, separately reviewed step acts on
the plan. Nothing in production calls it (default off): only scripts/org_lab_candidates.py, run by
hand with --check.

RECORD: {"id", "branch", "worktree", "created_at" (ISO 8601), "state"}, state one of
active / promoted / rejected / merged. POLICY: max_active, max_total (counts) and max_age_hours.

RULES.
  over_cap
      active count above max_active, or total count above max_total. Reason: the cap is the
      guard against sprawl; being over it is reported even when the sweep would fix it.
  sweepable (never an active candidate)
      older_than_max_age: not active and older than max_age_hours. Reason: a finished candidate
      that nobody promoted or merged has no further use and costs a branch and a worktree.
      over_max_total: when the total is above max_total, the oldest remaining non-active
      candidates beyond the cap. Reason: the cap must be restorable without touching live work.
      If non-active candidates cannot cover the excess, `unresolved_excess` says by how many; an
      active candidate is never proposed for removal, a human decides.
  violations (each one a code in the plan)
      branch_prefix: the branch does not start with "lab/". Reason: the prefix is how lab
      branches are told apart from real work, so a sweep never matches one by accident.
      worktree_outside_lab / worktree_is_lab_root / worktree_not_absolute /
      on_h_drive / unc_path: the worktree is not strictly inside the lab root, or sits on shared
      company storage. Reason: the production tree must never be the place candidates live.
      duplicate_id / duplicate_branch / duplicate_worktree: two records name the same thing, so
      a sweep of one would hit the other.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from relay.org import lab_isolation as _iso

VERSION = 1
BRANCH_PREFIX = "lab/"
STATES = ("active", "promoted", "rejected", "merged")


class LabCandidatesError(ValueError):
    """An input record, policy or timestamp is not usable."""


def parse_time(value, what="timestamp"):
    """ISO 8601 text -> aware datetime (naive input counts as UTC; a trailing Z is accepted)."""
    if not isinstance(value, str) or not value.strip():
        raise LabCandidatesError("%s must be a non-empty ISO 8601 string" % what)
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise LabCandidatesError("%s is not ISO 8601: %r" % (what, value))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _count(value, what):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LabCandidatesError("%s must be a non-negative integer" % what)
    return value


def normalize_policy(policy):
    """Validate a policy dict; returns a clean copy."""
    if not isinstance(policy, dict):
        raise LabCandidatesError("policy must be an object")
    age = policy.get("max_age_hours")
    if isinstance(age, bool) or not isinstance(age, (int, float)) or not age > 0:
        raise LabCandidatesError("max_age_hours must be a positive number")
    return {"max_active": _count(policy.get("max_active"), "max_active"),
            "max_total": _count(policy.get("max_total"), "max_total"),
            "max_age_hours": age}


def normalize_record(rec):
    """Validate one candidate record; returns a clean copy with a parsed `created`."""
    if not isinstance(rec, dict):
        raise LabCandidatesError("a candidate record must be an object")
    out = {}
    for key in ("id", "branch", "worktree"):
        v = rec.get(key)
        if not isinstance(v, str) or not v.strip():
            raise LabCandidatesError("record %r: %s must be a non-empty string" % (rec.get("id"), key))
        out[key] = v
    if rec.get("state") not in STATES:
        raise LabCandidatesError("record %r: state must be one of %s" % (out["id"], "/".join(STATES)))
    out["state"] = rec["state"]
    out["created_at"] = rec.get("created_at")
    out["created"] = parse_time(rec.get("created_at"), "record %r created_at" % out["id"])
    return out


def _dupes(recs, key):
    seen, dup = {}, []
    for r in recs:
        v = r[key]
        if v in seen and v not in dup:
            dup.append(v)
        seen[v] = r["id"]
    return dup


def plan_sweep(records, policy, lab_root, now, case_insensitive=None):
    """Deterministic plan: {"ok", "violations", "over_cap", "sweepable", "unresolved_excess", ...}.

    Pure and read-only apart from os.path.realpath on the worktree paths. `now` is an ISO string
    from the caller. Ordering is by (created_at, id), so equal input gives equal output.
    """
    if not isinstance(records, list):
        raise LabCandidatesError("records must be a list")
    pol = normalize_policy(policy)
    lab = _iso._path(lab_root, "lab_root")
    now_dt = parse_time(now, "now")
    ci = (os.name == "nt") if case_insensitive is None else bool(case_insensitive)
    recs = sorted((normalize_record(r) for r in records), key=lambda r: (r["created"], r["id"]))
    c_lab = _iso._canon(lab, ci)

    v = []

    def add(code, rid, detail):
        v.append({"code": code, "id": rid, "detail": detail})

    for key, code in (("id", "duplicate_id"), ("branch", "duplicate_branch"),
                      ("worktree", "duplicate_worktree")):
        for value in _dupes(recs, key):
            add(code, value if key == "id" else None, "more than one record has %s %r" % (key, value))
    for r in recs:
        if not r["branch"].startswith(BRANCH_PREFIX) or r["branch"] == BRANCH_PREFIX:
            add("branch_prefix", r["id"], "branch must start with %r and name a candidate" % BRANCH_PREFIX)
        wt = r["worktree"]
        if not os.path.isabs(wt):
            add("worktree_not_absolute", r["id"], "worktree path is not absolute")
            continue
        for code in _iso._shared_storage(wt, _iso._real(wt)):
            add(code, r["id"], "worktree is on shared company storage")
        rel = _iso._relation(_iso._canon(wt, ci), c_lab)
        if rel == "same":
            add("worktree_is_lab_root", r["id"], "worktree is the lab root itself")
        elif rel != "inside":
            add("worktree_outside_lab", r["id"], "worktree is not inside the lab root")

    active = [r for r in recs if r["state"] == "active"]
    over = []
    if len(active) > pol["max_active"]:
        over.append({"cap": "max_active", "count": len(active), "limit": pol["max_active"],
                     "excess": len(active) - pol["max_active"]})
    if len(recs) > pol["max_total"]:
        over.append({"cap": "max_total", "count": len(recs), "limit": pol["max_total"],
                     "excess": len(recs) - pol["max_total"]})

    cutoff = now_dt - timedelta(hours=pol["max_age_hours"])
    sweep = {}
    for r in recs:
        if r["state"] != "active" and r["created"] < cutoff:
            sweep[r["id"]] = "older_than_max_age"
    excess = max(0, len(recs) - pol["max_total"])
    remaining = len(recs) - len(sweep)
    if remaining > pol["max_total"]:
        need = remaining - pol["max_total"]
        for r in recs:  # oldest first
            if need == 0:
                break
            if r["state"] != "active" and r["id"] not in sweep:
                sweep[r["id"]] = "over_max_total"
                need -= 1
    unresolved = max(0, len(recs) - len(sweep) - pol["max_total"]) if excess else 0
    sweepable = [{"id": r["id"], "branch": r["branch"], "worktree": r["worktree"],
                  "state": r["state"], "created_at": r["created_at"], "reason": sweep[r["id"]]}
                 for r in recs if r["id"] in sweep]
    return {"version": VERSION, "ok": not v and not over, "now": now_dt.isoformat(),
            "lab_root": c_lab, "policy": pol, "count": len(recs), "active": len(active),
            "violations": v, "over_cap": over, "sweepable": sweepable,
            "unresolved_excess": unresolved}


def to_json(obj):
    """Stable JSON text for a plan."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, indent=2)
