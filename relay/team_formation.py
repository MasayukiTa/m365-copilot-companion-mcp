# -*- coding: utf-8 -*-
"""Dynamic team formation, SHADOW ONLY: at split time it records the team a team-forming rule
would have proposed, next to the team the static split actually built. Nothing else.

WHERE IT SITS. A worker asked to split replies with numbered steps; relay/fanout.py turns them
into children (role `subtask`) and relay/relay_fleet.py queues them, and one aggregator merges
them later. That team shape is fixed: N identical subtasks plus one aggregator. This module takes
the shape of the split (how many steps, how short they are, how deep in the tree) and the role
profile (scripts/capability_profile.py, slice C1) and says which roles and how many members a
team-forming rule would have chosen instead. It writes that beside what the static split did.

WHAT THIS MODULE IS NOT. It never changes the steps, the children, a prompt, the queue or the
merge. The setting `team_mode` accepts `off` and `shadow` and nothing else: there is no value
that forms a team, so there is nothing to set by mistake. Acting on a template would be a later
change with its own review, once scripts/team_formation_report.py says there is enough data. In
`off` nothing here reads a file or records.

THE RULES IT RECORDS (kept small on purpose and stated so a reader can disagree with them).
The static team is {subtask: N, aggregator: 1}. The proposed team starts as that and:
  * fold_tiny   Steps shorter than TINY_STEP_CHARS characters are folded in pairs into one child,
                because a slice that small costs a whole conversation. Never below MIN_CHILDREN.
  * review      When the profile labels the success of `subtask` OR of `aggregator` as poor
                (never when it is unknown: too few runs to say), one `reviewer` is added.
A profile that is unknown changes nothing, and the row says so (`profile_known`), so the report
can tell "the rule agrees with the static team" apart from "the rule had nothing to go on".

HONESTY RULES. A write error is counted and swallowed: the recorder runs inside the worker's
split decision and must never raise into it. The file is bounded (one rotated copy) and
append-only. A row holds role names, counts, lengths and rule names only, never goal or step text.
"""
from __future__ import annotations

import io
import json
import os
import time

KEY = "team_mode"
#: ONLY these. There is deliberately no value that forms a team (see the module docstring).
MODES = ("off", "shadow")

LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   ".fleet", "team_formation_shadow.jsonl")
#: The log is rotated to `<LOG>.1` (replacing the old copy) when it reaches this size, so the
#: records on disk never exceed about twice this.
MAX_BYTES = 1024 * 1024
#: How long a derived profile is reused (it reads campaigns.jsonl, history.json, status.json).
PROFILE_TTL_S = 600.0
#: A step shorter than this many characters is "tiny" for the fold_tiny rule.
TINY_STEP_CHARS = 40
#: The static split never makes fewer children than this (relay/fanout.py MIN_CHILDREN).
MIN_CHILDREN = 2
#: Splits remembered so that a retried split of one campaign is one row. Bounds memory.
SEEN_MAX = 2048
SCHEMA = 1

R_SUB, R_AGG, R_REV = "subtask", "aggregator", "reviewer"
POOR = "poor"

_STATE = {"recorded": 0, "differed": 0, "write_errors": 0}
_seen = set()
_settings_cache = {}
_profile_cache = {"at": None, "dir": None, "profiles": {}}


# ------------------------------------------------------------------ the setting

def mode(settings_path=None):
    """`team_mode=` from settings.txt: "shadow" or "off". Anything else reads as off.
    Re-read when the file changes (each_gate). Never raises."""
    try:
        path = settings_path
        if path is None:
            from tools.settings_path import settings_file
            path = settings_file()
        if not path:
            return "off"
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
        hit = _settings_cache.get(path)
        if hit is not None and hit[0] == key:
            return hit[1]
        val = "off"
        with io.open(path, encoding="utf-8-sig") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln.startswith(KEY + "="):
                    v = ln.split("=", 1)[1].strip().lower()
                    val = v if v in MODES else "off"
                    break
        _settings_cache[path] = (key, val)
        return val
    except Exception:
        return "off"


# ------------------------------------------------------------------ the profile

def profiles(now=None):
    """{role: capability_profile entry}, cached for PROFILE_TTL_S. {} when it cannot be read."""
    try:
        t = time.time() if now is None else now
        # Derived from LOG's directory at call time, so a test that redirects LOG redirects the
        # profile source with it and never reads the operator's records.
        fdir = os.path.dirname(LOG)
        c = _profile_cache
        if c["at"] is not None and c["dir"] == fdir and 0 <= t - c["at"] < PROFILE_TTL_S:
            return c["profiles"]
        from scripts import capability_profile
        c.update({"at": t, "dir": fdir, "profiles": capability_profile.load_profiles(fdir)})
        return c["profiles"]
    except Exception:
        return {}


def _success(prof, role):
    s = ((prof or {}).get(role) or {}).get("success") or {}
    return {"label": str(s.get("label") or "unknown"), "rate": s.get("rate"), "n": s.get("n")}


# ------------------------------------------------------------------ the proposal (pure)

def propose(step_lens, prof):
    """(actual_roles, proposed_roles, rules, profile_known) for a split with these step lengths.

    The actual team is what the static split builds. The proposed team is the rule's. Pure."""
    n = len(step_lens)
    actual = {R_SUB: n, R_AGG: 1}
    proposed = dict(actual)
    rules = []
    tiny = sum(1 for k in step_lens if k < TINY_STEP_CHARS)
    folded = min(tiny // 2, max(0, n - MIN_CHILDREN))
    if folded > 0:
        proposed[R_SUB] = n - folded
        rules.append("fold_tiny")
    sub, agg = _success(prof, R_SUB), _success(prof, R_AGG)
    known = sub["label"] != "unknown" or agg["label"] != "unknown"
    if POOR in (sub["label"], agg["label"]):
        proposed[R_REV] = 1
        rules.append("review")
    return actual, proposed, rules, known


# ------------------------------------------------------------------ the recorder

def _append(row):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        try:
            if os.path.getsize(LOG) >= MAX_BYTES:
                os.replace(LOG, LOG + ".1")
        except OSError:
            pass
        with io.open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        return True
    except Exception:
        _STATE["write_errors"] += 1
        return False


def shadow_propose(kids, steps, run_id="", settings_path=None, now=None):
    """Called by the worker once per split, after the children are built and before they are
    queued. In `shadow` it appends one row: the team the static split built and the team the rule
    would have proposed. In `off` it returns at once. Never raises, never mutates its arguments,
    returns nothing."""
    try:
        if mode(settings_path) != "shadow":
            return None
        first = (kids or [None])[0] or {}
        cid = str(first.get("campaign_id") or "")
        ident = (str(run_id or ""), cid)
        if cid:
            if ident in _seen:
                return None          # a retried split of one campaign: one row
            if len(_seen) >= SEEN_MAX:
                _seen.clear()
            _seen.add(ident)
        lens = [len(str(s or "")) for s in (steps or [])]
        if not lens:
            return None
        prof = profiles(now)
        actual, proposed, rules, known = propose(lens, prof)
        differs = actual != proposed
        row = {
            "v": SCHEMA, "ts": round(time.time() if now is None else now, 1),
            "run_id": str(run_id or ""), "mode": "shadow", "campaign_id": cid,
            "depth": int(first.get("depth") or 1),
            "actual": {"roles": actual, "members": sum(actual.values())},
            "proposed": {"roles": proposed, "members": sum(proposed.values())},
            "differs": differs, "rules": rules, "profile_known": known,
            "shape": {"steps": len(lens), "min_chars": min(lens), "max_chars": max(lens),
                      "tiny": sum(1 for k in lens if k < TINY_STEP_CHARS)},
            "profile": {R_SUB: _success(prof, R_SUB), R_AGG: _success(prof, R_AGG)},
        }
        if _append(row):
            _STATE["recorded"] += 1
            _STATE["differed"] += 1 if differs else 0
        return None
    except Exception:
        return None


def status_block(settings_path=None):
    """{"team_mode": {mode, recorded, differed}} for status.json (additive)."""
    return {KEY: {"mode": mode(settings_path), "recorded": int(_STATE["recorded"]),
                  "differed": int(_STATE["differed"])}}


def _reset_for_test():
    _STATE.update({"recorded": 0, "differed": 0, "write_errors": 0})
    _seen.clear()
    _settings_cache.clear()
    _profile_cache.update({"at": None, "dir": None, "profiles": {}})
