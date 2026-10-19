# -*- coding: utf-8 -*-
"""Load-aware routing of queued goals, SHADOW ONLY: it records the choice it would have made.

WHERE IT SITS. The coordinator admits queued goals one at a time (relay/relay_fleet.py, the
`while pending:` loop of run_relay_fleet). Today that is queue order, gated by the admission
margins: the lane cap (`maxtabs`), the RAM safeguard (relay/socket_capacity.py), the disk floor
and the rate ceiling (`rate_ceiling_rpm`). This module takes the margins that are left at the
moment of one admission, together with the capability profile of each queued goal's ROLE
(scripts/capability_profile.py, slice C1), and says which queued goal it would have admitted
instead. It writes that next to what the coordinator actually admitted. Nothing else.

WHAT THIS MODULE IS NOT. It never reorders the queue, never picks a worker, never edits a prompt
and never changes an output. The setting `fanout_load_routing` accepts `off` and `shadow` and
nothing else; there is no routing value to set by mistake. A router that acts would be a later
change with its own review, once the report (scripts/load_routing_report.py) says there is enough
data. In `off` nothing here reads a file or records.

THE RULE IT RECORDS (kept small on purpose, and stated so a reader can disagree with it).
  * With a single queued goal there is no choice: the shadow choice is the actual one.
  * A goal's expected cost is the mean turns of its role in the profile, and only when that
    profile is labelled good or poor (never `unknown`: too few runs to say).
  * A comparison needs BOTH numbers. If the goal the coordinator admitted has an unknown cost,
    the shadow choice is that same goal and the reason says why. Unknown is not "cheap".
  * Margins tight (one slot or fewer left under the cap, or the rate gate says hold): prefer the
    cheapest known goal, so the slot is freed sooner. Margins loose: prefer the most expensive
    known goal, so long work starts while capacity is free. Ties keep queue order.

HONESTY RULES. A write error is counted and swallowed: the recorder runs inside the admission
loop and must never raise into it. The file is bounded (one rotated copy), append-only. A row
holds names, roles and numbers only, never goal text.
"""
from __future__ import annotations

import io
import json
import os
import time

KEY = "fanout_load_routing"
#: ONLY these. There is deliberately no value that routes (see the module docstring).
MODES = ("off", "shadow")

LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   ".fleet", "load_routing_shadow.jsonl")
#: The log is rotated to `<LOG>.1` (replacing the old copy) when it reaches this size, so the
#: records on disk never exceed about twice this.
MAX_BYTES = 1024 * 1024
#: How long a derived profile is reused. Deriving it reads campaigns.jsonl, history.json and
#: status.json; once per ten minutes is far below the admission rate.
PROFILE_TTL_S = 600.0
#: Candidates written into one row (the head of the queue first). Bounds a row's size.
MAX_CANDIDATES_LOGGED = 12
SCHEMA = 1

_STATE = {"recorded": 0, "differed": 0, "comparable": 0, "write_errors": 0}
_settings_cache = {}
_profile_cache = {"at": None, "dir": None, "profiles": {}}


# ------------------------------------------------------------------ the setting

def mode(settings_path=None):
    """`fanout_load_routing=` from settings.txt: "shadow" or "off". Anything else reads as off.
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

def _fleet_dir():
    # Derived from LOG at call time, so a test that redirects LOG redirects the profile source
    # with it and never reads the operator's records.
    return os.path.dirname(LOG)


def profiles(now=None):
    """{role: capability_profile entry}, cached for PROFILE_TTL_S. {} when it cannot be read."""
    try:
        t = time.time() if now is None else now
        fdir = _fleet_dir()
        c = _profile_cache
        if c["at"] is not None and c["dir"] == fdir and 0 <= t - c["at"] < PROFILE_TTL_S:
            return c["profiles"]
        from scripts import capability_profile
        c.update({"at": t, "dir": fdir, "profiles": capability_profile.load_profiles(fdir)})
        return c["profiles"]
    except Exception:
        return {}


def role_of(worker):
    """The role a queued worker will run as. A worker without an envelope is a top-level goal."""
    try:
        r = getattr(getattr(worker, "task_envelope", None), "role", "") or ""
        return str(r).lower() or "producer"
    except Exception:
        return "producer"


def expected(role, prof):
    """{turns, success, confidence}: turns is None unless the role's profile can state a mean."""
    e = (prof or {}).get(role) or {}
    t = e.get("turns") or {}
    s = e.get("success") or {}
    mean = t.get("mean") if t.get("label") in ("good", "poor") else None
    return {"turns": mean, "success": s.get("rate"), "confidence": e.get("confidence", "unknown")}


# ------------------------------------------------------------------ the decision (pure)

def choose(cands, slots, rate_ok, prof):
    """cands: [{"name","role"}], the goal the coordinator admitted FIRST, then the rest of the
    queue in order. Returns {"index", "tight", "comparable", "reason"}; index 0 means "the same
    goal the coordinator admitted"."""
    tight = (slots is not None and slots <= 1) or rate_ok is False
    if len(cands) < 2:
        return {"index": 0, "tight": tight, "comparable": False, "reason": "single candidate"}
    costs = [expected(c["role"], prof)["turns"] for c in cands]
    if costs[0] is None:
        return {"index": 0, "tight": tight, "comparable": False,
                "reason": "admitted goal's role profile unknown"}
    known = [(i, c) for i, c in enumerate(costs) if c is not None]
    comparable = len({c for _, c in known}) >= 2
    if tight:
        best = min(known, key=lambda ic: (ic[1], ic[0]))
        why = "margins tight: cheapest known first"
    else:
        best = max(known, key=lambda ic: (ic[1], -ic[0]))
        why = "margins loose: most expensive known first"
    if best[1] == costs[0]:
        best = known[0]            # a tie keeps queue order
        why = "no known cost differs from the admitted goal's"
    return {"index": best[0], "tight": tight, "comparable": comparable, "reason": why}


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


def _probe(probes, name):
    try:
        fn = (probes or {}).get(name)
        return fn() if fn else None
    except Exception:
        return None


def shadow_assign(admitted, remaining, active_open, cap, lane_ceiling, run_id="",
                  ram_floor_mb=None, probes=None, settings_path=None, now=None):
    """Called by the coordinator once per admission, after the goal is chosen and before it is
    attached. In `shadow` it appends one row: the goal actually admitted, the goal this router
    would have admitted, and the margins that decided it. In `off` it returns at once.
    Never raises, never mutates its arguments, returns nothing."""
    try:
        if mode(settings_path) != "shadow":
            return None
        # A goal whose attach failed goes back to the head of the queue and is admitted again on
        # a later sweep: one admission, one row.
        ident = (str(run_id or ""), str(getattr(admitted, "name", "") or ""))
        if _STATE.get("last") == ident:
            return None
        _STATE["last"] = ident
        prof = profiles(now)
        ws = [admitted] + list(remaining or [])
        cands = [{"name": str(getattr(w, "name", "") or ""), "role": role_of(w)} for w in ws]
        slots = None if cap is None or active_open is None else int(cap) - int(active_open)
        rate = _probe(probes, "rate_ok")
        rate_ok = rate[0] if isinstance(rate, tuple) and rate else rate
        verdict = choose(cands, slots, rate_ok, prof)
        pick = cands[verdict["index"]]
        differs = verdict["index"] != 0
        row = {
            "v": SCHEMA, "ts": round(time.time() if now is None else now, 1),
            "run_id": str(run_id or ""), "mode": "shadow",
            "actual": cands[0], "shadow": pick, "differs": differs,
            "comparable": verdict["comparable"], "tight": verdict["tight"],
            "reason": verdict["reason"], "queue_len": len(cands),
            "headroom": {"active": active_open, "cap": cap, "lane_ceiling": lane_ceiling,
                         "slots": slots, "rate_ok": rate_ok,
                         "rate_why": (rate[1] if isinstance(rate, tuple) and len(rate) > 1
                                      else ""),
                         "avail_mb": _probe(probes, "avail_mb"), "ram_floor_mb": ram_floor_mb},
            "candidates": [dict(c, **expected(c["role"], prof))
                           for c in cands[:MAX_CANDIDATES_LOGGED]],
        }
        if _append(row):
            _STATE["recorded"] += 1
            _STATE["differed"] += 1 if differs else 0
            _STATE["comparable"] += 1 if verdict["comparable"] else 0
        return None
    except Exception:
        return None


def status_block(settings_path=None):
    """{"fanout_load_routing": {mode, recorded, differed}} for status.json (additive)."""
    return {KEY: {"mode": mode(settings_path), "recorded": int(_STATE["recorded"]),
                  "differed": int(_STATE["differed"])}}


def _reset_for_test():
    _STATE.update({"recorded": 0, "differed": 0, "comparable": 0, "write_errors": 0, "last": None})
    _settings_cache.clear()
    _profile_cache.update({"at": None, "dir": None, "profiles": {}})
