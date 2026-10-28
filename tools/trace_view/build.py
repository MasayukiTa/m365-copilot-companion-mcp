# -*- coding: utf-8 -*-
"""Turn loaded records into per-session traces: steps, gaps, refusals, findings, statistics.

`build(loaded)` returns one plain, JSON-serialisable dict (the "report") that every renderer
consumes. Every string that can reach an output has already been through `redact`, so a
renderer never has to know what a secret looks like.
"""
from __future__ import annotations

import collections

from . import load as _load
from .origin_attribution import attribute_by_origin, dominant_origin
from . import redact as R

try:  # the ledger's own verdict helpers; the viewer still works if they cannot be imported
    from tools.tool_ledger import looks_failed, looks_refused, looks_unavailable
except Exception:  # pragma: no cover - defensive
    def looks_failed(_t):
        return False

    def looks_refused(_t):
        return False

    def looks_unavailable(_t):
        return False

DEFAULT_GAP_S = 120.0

#: Refusal reason codes, in the order the spec lists them.
REASONS = ("locked", "env_protected", "protected_path", "unknown_tool", "missing_args",
           "unexpected_args", "duplicate", "limit_exceeded", "control_message")

_SUBMIT_LOG_REASON = {"similar": "duplicate", "too_long": "limit_exceeded",
                      "pending_full": "limit_exceeded", "missing_goal": "missing_args",
                      "local_loop": "control_message", "other": "error"}

#: Gateway pseudo-tools: discovery chatter rather than work.
def _is_discovery(tool: str) -> bool:
    return tool.startswith("call_tool.")


def _otext(outcome) -> str:
    if not outcome:
        return ""
    res = outcome.get("result")
    if isinstance(res, dict):
        return str(res.get("text") or "")
    return str(res or "")


def classify(tool: str, outcome, text: str, err: str):
    """(status, reason). status is ok / error / refused / unavailable / no_outcome."""
    if outcome is None:
        return "no_outcome", ""
    low_err = err.lower()
    head = text.lstrip()[:500]
    if low_err.startswith("unavailable") or looks_unavailable(text) or outcome.get("unavailable"):
        return "unavailable", ""
    if low_err.startswith("refused (locked)") or looks_refused(text):
        return "refused", "locked"
    if tool == "call_tool.unknown" or "no tool or category named" in head:
        return "refused", "unknown_tool"
    if "missing required" in low_err[:300] or ("[call_tool" in head and "missing required" in head):
        return "refused", "missing_args"
    if "does not accept" in low_err[:300] or ("[call_tool" in head and "does not accept" in head):
        return "refused", "unexpected_args"
    if "Refusing to touch" in head or "Refusing to touch" in err:
        env = "(.env)" in head or "(.env)" in err
        return "refused", ("env_protected" if env else "protected_path")
    if tool == "fleet_submit":
        if head.startswith("queued "):
            return "ok", ""
        if head.startswith("[fleet_submit"):
            if "already queued or running" in head or "already queued" in head:
                return "refused", "duplicate"
            if "over the" in head and "limit" in head:
                return "refused", "limit_exceeded"
            if "jobs are already waiting" in head:
                return "refused", "limit_exceeded"
            if "empty goal" in head:
                return "refused", "missing_args"
            if "LOCAL_LOOP" in head:
                return "refused", "control_message"
            return "error", ""
    if "429" in head[:80] or "rate limit" in head[:200].lower() or "too many requests" in head[:200].lower():
        return "refused", "limit_exceeded"
    if err or not outcome.get("ok", True) or looks_failed(text):
        return "error", ""
    return "ok", ""


def _duration(call, outcome):
    if not outcome:
        return None
    for key in ("duration_s", "dur_mono_s"):
        v = outcome.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    t0 = outcome.get("ts_start", call.get("ts"))
    t1 = outcome.get("ts_end", outcome.get("ts"))
    if isinstance(t0, (int, float)) and isinstance(t1, (int, float)):
        return max(0.0, float(t1) - float(t0))
    return None


def _make_step(call, outcome):
    tool = R.scrub(R.flat(call.get("tool") or "", 60))
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    text = _otext(outcome)
    err = str(outcome.get("error") or "") if outcome else ""
    status, reason = classify(str(call.get("tool") or ""), outcome, text, err)
    start = float(outcome.get("ts_start")) if outcome and isinstance(outcome.get("ts_start"), (int, float)) \
        else float(call["ts"])
    dur = _duration(call, outcome)
    end = start + (dur or 0.0)
    withheld = R.args_mention_env(args) or str(call.get("tool")) in R.WITHHELD_RESULT_TOOLS
    step = {
        "id": str(call.get("id")), "start": start, "end": end, "dur": dur, "tool": tool,
        "discovery": _is_discovery(str(call.get("tool") or "")),
        "status": status, "reason": reason,
        "args": R.summarize_args(args),
        "error": R.scrub(R.flat(err, 80)),
        "result": R.result_head(call.get("tool"), args, text),
        "withheld": bool(withheld and text),
        "worker": R.scrub(R.flat(call.get("worker") or "", 40)),
        "task": R.scrub(R.flat(call.get("task") or "", 40)),
        "lock_logged": False, "submit": None,
    }
    if str(call.get("tool")) == "fleet_submit" and outcome is not None:
        head = text.lstrip()
        if head.startswith("queued "):
            parts = head.split()
            step["submit"] = {"result": "queued", "reason": "",
                              "job": R.scrub(parts[1][:16]) if len(parts) > 1 else ""}
        elif status == "refused":
            step["submit"] = {"result": "refused", "reason": reason, "job": ""}
        else:
            step["submit"] = {"result": "error", "reason": reason, "job": ""}
    return step


def _max_overlap(steps):
    marks = []
    for s in steps:
        marks.append((s["start"], 1))
        marks.append((max(s["end"], s["start"] + 0.001), -1))
    marks.sort(key=lambda m: (m[0], m[1]))
    cur = best = 0
    for _t, d in marks:
        cur += d
        best = max(best, cur)
    return best


def find_gaps(steps, gap_s):
    """Idle stretches: from the end of the latest-ending earlier call to the next call's start."""
    out = []
    ordered = sorted(steps, key=lambda s: s["start"])
    prev_end, prev = None, None
    for s in ordered:
        if prev is not None:
            idle = s["start"] - prev_end
            if idle >= gap_s:
                # shown_s is what a reader gets by subtracting the two clock times as displayed
                # (whole seconds), so the label never disagrees with the times printed beside it.
                out.append({"start": prev_end, "end": s["start"], "seconds": idle,
                            "shown_s": int(s["start"]) - int(prev_end),
                            "after_tool": prev["tool"], "after_id": prev["id"],
                            "before_tool": s["tool"], "before_id": s["id"],
                            "after_status": prev["status"], "before_status": s["status"]})
        if prev_end is None or s["end"] > prev_end:
            prev_end, prev = s["end"], s
    return out


def _stats(steps):
    by_tool = collections.OrderedDict()
    for s in steps:
        t = by_tool.setdefault(s["tool"], {"calls": 0, "ok": 0, "error": 0, "refused": 0,
                                           "unavailable": 0, "no_outcome": 0, "total_s": 0.0,
                                           "max_s": 0.0})
        t["calls"] += 1
        t[s["status"]] += 1
        d = s["dur"] or 0.0
        t["total_s"] += d
        t["max_s"] = max(t["max_s"], d)
    for t in by_tool.values():
        t["total_s"] = round(t["total_s"], 3)
        t["max_s"] = round(t["max_s"], 3)
    status = collections.Counter(s["status"] for s in steps)
    reasons = collections.Counter(s["reason"] for s in steps if s["reason"])
    return {"by_tool": by_tool, "status": dict(status), "reasons": dict(reasons)}


def _attach_lock_log(sessions, lock_rows, events_global):
    for row in lock_rows:
        sid = str(row.get("session") or "")
        sess = sessions.get(sid)
        ts = row.get("ts", 0)
        kind = row.get("event")
        if kind == "refused" and sess:
            for st in sess["steps"]:
                if st["reason"] == "locked" and not st["lock_logged"] \
                        and (abs(ts - st["end"]) <= 3.0 or abs(ts - st["start"]) <= 3.0):
                    st["lock_logged"] = True
                    break
            else:
                sess["events"].append({"ts": ts, "kind": "lock_refused", "site": R.scrub(R.flat(row.get("site"), 40)),
                                       "state": R.scrub(R.flat(row.get("session_state") or "", 30))})
        elif kind == "granted":
            ev = {"ts": ts, "kind": "unlock_granted", "via": R.scrub(R.flat(row.get("via") or "", 20))}
            (sess["events"] if sess else events_global).append(ev)
        elif kind == "refused":
            events_global.append({"ts": ts, "kind": "lock_refused", "site": R.scrub(R.flat(row.get("site"), 40)),
                                  "state": R.scrub(R.flat(row.get("session_state") or "", 30)),
                                  "session": sid})


def _attach_submit_log(sessions, rows, events_global):
    steps = [s for sess in sessions.values() for s in sess["steps"] if s["submit"]]
    for row in rows:
        ts = row.get("ts", 0)
        raw = str(row.get("reason") or "other")
        reason = _SUBMIT_LOG_REASON.get(raw, "error")
        best = None
        for st in steps:
            if st["submit"]["result"] == "queued" or st["submit"].get("logged"):
                continue
            d = abs(ts - st["end"])
            if d <= 10.0 and (best is None or d < abs(ts - best["end"])):
                best = st
        if best is not None:
            best["submit"]["logged"] = raw
            if not best["reason"]:
                best["reason"] = reason
                best["submit"]["reason"] = reason
        else:
            events_global.append({"ts": ts, "kind": "submit_refused_not_in_ledger", "reason": reason,
                                  "goal_len": row.get("goal_len")})


def _findings(sid, sess, gap_s):
    out = []
    steps = sess["steps"]
    for g in sess["gaps"]:
        out.append({"session": sid, "kind": "gap", "ts": g["start"], "seconds": g["shown_s"],
                    "detail": "%s -> %s" % (g["after_tool"], g["before_tool"])})
    locked = [s for s in steps if s["reason"] == "locked"]
    submits = [s for s in steps if s["submit"]]
    if locked and not submits:
        out.append({"session": sid, "kind": "locked_without_submit", "ts": locked[0]["start"],
                    "detail": "%d locked refusal(s), no fleet_submit in this session" % len(locked)})
    elif locked and submits and not any(s["submit"]["result"] == "queued" for s in submits):
        out.append({"session": sid, "kind": "locked_submit_not_queued", "ts": locked[0]["start"],
                    "detail": "%d locked refusal(s); fleet_submit never queued" % len(locked)})
    if sess["connect_only"]:
        out.append({"session": sid, "kind": "connect_without_call", "ts": sess["first"],
                    "detail": ", ".join(sess["connect"]["methods"])})
    orphans = [s for s in steps if s["status"] == "no_outcome"]
    if orphans:
        out.append({"session": sid, "kind": "call_without_outcome", "ts": orphans[0]["start"],
                    "detail": "%d call(s) with no outcome (e.g. %s)" % (len(orphans), orphans[0]["tool"])})
    run, prev = 0, None
    for s in steps:
        key = s["reason"] if s["status"] == "refused" else None
        if key and key == prev:
            run += 1
        else:
            if prev and run >= 3:
                out.append({"session": sid, "kind": "repeated_refusal", "ts": s["start"],
                            "detail": "%s x%d" % (prev, run)})
            run, prev = (1, key) if key else (0, None)
    if prev and run >= 3:
        out.append({"session": sid, "kind": "repeated_refusal", "ts": steps[-1]["start"],
                    "detail": "%s x%d" % (prev, run)})
    inbound_calls = sess["connect"]["tool_calls"]
    if inbound_calls and inbound_calls > len(steps) + 1:
        out.append({"session": sid, "kind": "inbound_calls_without_ledger", "ts": sess["first"],
                    "detail": "%d tools/call arrivals, %d ledger calls" % (inbound_calls, len(steps))})
    return out


def build(loaded: dict, since=None, until=None, gap_s: float = DEFAULT_GAP_S) -> dict:
    calls, outcomes = loaded.get("calls", {}), loaded.get("outcomes", {})
    sessions = {}

    def sess_of(sid):
        return sessions.setdefault(sid, {"id": sid, "steps": [], "events": [], "gaps": [],
                                         "inbound": []})

    orphan_outcomes = sum(1 for k in outcomes if k not in calls)
    for cid, call in sorted(calls.items(), key=lambda kv: kv[1].get("ts", 0)):
        step = _make_step(call, outcomes.get(cid))
        sess_of(str(call.get("session") or ""))["steps"].append(step)

    inbound = _load.attribute_inbound(loaded.get("inbound", []))
    for row in inbound:
        sess_of(str(row.get("session") or ""))["inbound"].append(row)
    origin = attribute_by_origin(inbound)
    origin["dominant"] = dominant_origin(origin)
    events_global = []
    _attach_lock_log(sessions, loaded.get("lock", []), events_global)
    _attach_submit_log(sessions, loaded.get("submit_refusals", []), events_global)
    for row in loaded.get("send_stage", []):
        events_global.append({"ts": row.get("ts", 0), "kind": "send_stage",
                              "stage": R.scrub(R.flat(row.get("stage") or "", 30))})

    all_steps = [s for sess in sessions.values() for s in sess["steps"]]
    out_sessions = []
    for sid, sess in sessions.items():
        steps = sorted(sess["steps"], key=lambda s: s["start"])
        for i, s in enumerate(steps, 1):
            s["n"] = i
        sess["steps"] = steps
        methods = collections.OrderedDict()
        for r in sess["inbound"]:
            methods[str(r.get("method") or "")[:60]] = methods.get(str(r.get("method") or "")[:60], 0) + 1
        tool_calls = methods.get("tools/call", 0)
        ua = R.scrub(R.flat((sess["inbound"][0].get("ua") if sess["inbound"] else "") or "", 40))
        for r in sess["inbound"]:
            if r.get("method") != "tools/call":
                sess["events"].append({"ts": r.get("ts", 0), "kind": "connect",
                                       "method": R.scrub(R.flat(r.get("method"), 40))})
        sess["connect"] = {"methods": list(methods.keys()), "counts": dict(methods),
                           "tool_calls": tool_calls, "ua": ua,
                           "first": sess["inbound"][0].get("ts") if sess["inbound"] else None}
        sess["connect_only"] = bool(sess["inbound"]) and tool_calls == 0 and not steps
        sess["events"].sort(key=lambda e: e["ts"])
        sess["gaps"] = find_gaps(steps, gap_s)
        own = {x["id"] for x in steps}
        for g in sess["gaps"]:
            g["other_sessions_calls"] = sum(
                1 for s in all_steps if s["id"] not in own and g["start"] <= s["start"] <= g["end"])
            g["events_inside"] = [e["kind"] for e in sess["events"] if g["start"] <= e["ts"] <= g["end"]]
        times = [s["start"] for s in steps] + [e["ts"] for e in sess["events"]]
        sess["first"] = min(times) if times else (sess["connect"]["first"] or 0)
        sess["last"] = max([s["end"] for s in steps] + [e["ts"] for e in sess["events"]] or [sess["first"]])
        overlap = _max_overlap(steps) if steps else 0
        sess["concurrency"] = {"max_overlap": overlap,
                               "mode": "parallel" if overlap > 1 else ("serial" if steps else "none")}
        sess["stats"] = _stats(steps)
        sess["submits"] = [{"n": s["n"], "start": s["start"], **s["submit"]} for s in steps if s["submit"]]
        sess["findings"] = _findings(sid, sess, gap_s)
        del sess["inbound"]
        out_sessions.append(sess)
    out_sessions.sort(key=lambda s: (s["first"], s["id"]))
    findings = [f for s in out_sessions for f in s["findings"]]
    findings.sort(key=lambda f: f["ts"])
    health = loaded.get("health", {})
    return {
        "meta": {"since": since, "until": until, "gap_s": gap_s, "n_sessions": len(out_sessions),
                 "n_steps": len(all_steps), "orphan_outcomes": orphan_outcomes,
                 "health": health},
        "sessions": out_sessions,
        "origin_attribution": origin,
        "global_gaps": find_gaps(all_steps, gap_s),
        "stats": _stats(all_steps),
        "findings": findings,
        "events_global": sorted(events_global, key=lambda e: e["ts"]),
    }


def filter_sessions(report: dict, prefix: str) -> dict:
    """The same report restricted to sessions whose id starts with `prefix`."""
    keep = [s for s in report["sessions"] if s["id"].startswith(prefix)]
    out = dict(report)
    out["sessions"] = keep
    out["findings"] = [f for f in report["findings"] if f["session"].startswith(prefix)]
    return out
