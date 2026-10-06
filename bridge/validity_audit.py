# -*- coding: utf-8 -*-
"""The audit ledger of the validity tools: the whole conversation, bound to a claim id.

WHY. A validity verdict (GO / HOLD / NA for a pre-registered claim id) is only usable if what
produced it can be audited. Before this the pieces were scattered and partly absent: the goal and
the worker's text in fleet_turns, the tool calls in .fleet/tool_events.jsonl (arguments hashed,
results cut to 2000 characters), the reviewer's conversation nowhere. Nothing tied them to the
claim id. `validity_audit` (bridge/session_store.py) holds one row per piece, tagged by role and
bound to a claim id, and `validity_audit_ledger(claim_id)` reads it back in time order.

ROLE TAGS
    goal                 the goal text the worker was started with
    worker_prompt        a turn sent to the worker (the job as composed)
    worker_prompt_wire   the same turn as actually put on the wire (preamble / tools included)
    worker_reply         the worker's answer
    tool_call            a validity_* tool call (name and arguments)
    tool_result          what it returned (as the tool ledger kept it: <= 2000 characters)
    refuter_prompt       the full text the reviewer was sent
    refuter_prompt_wire  the payload the socket transport really sent to it
    refuter_reply        a reply the reviewer settled on
    verdict              the review's verdict as the fleet took it
    outcome              the worker's final outcome

A conversation whose claim cannot be identified is bound to 'UNATTRIBUTED'. It is never dropped.
Rows from before 2026-10-06 have no reviewer text; that is written as an explicit
`not_recorded(before 2026-10-06)` row rather than left as a silent absence.

IDEMPOTENT. The unique key includes the source row, so running this again (the live hook at worker
close, and the backfill over the same night) adds only what is new.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

UNATTRIBUTED = "UNATTRIBUTED"
NOT_RECORDED = "not_recorded(before 2026-10-06)"

_ROLE_OF = {
    "meta": "goal", "user": "worker_prompt", "user_wire": "worker_prompt_wire",
    "assistant": "worker_reply", "refuter_user": "refuter_prompt",
    "refuter_wire": "refuter_prompt_wire", "refuter_assistant": "refuter_reply",
    "refuter_verdict": "verdict",
}
_GOAL_CLAIM_RE = re.compile(r"分析ID\s*[「『\"']?([A-Za-z0-9_-]{1,64})")
_KEY_RE = re.compile(r"^(?P<run>.+)_(?P<name>w\d+)$")


def _arg_text(args, name):
    v = (args or {}).get(name)
    if isinstance(v, dict):
        return str(v.get("text") or "")
    return "" if v is None else str(v)


def _read_events(path, tail_bytes=None):
    """validity_* calls and their outcomes from the tool ledger: ({id: call}, {id: outcome})."""
    calls, outs, wanted = {}, {}, set()
    if not path or not os.path.isfile(path):
        return calls, outs
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if tail_bytes and size > tail_bytes:
            fh.seek(size - tail_bytes)
            fh.readline()                      # drop the partial line the seek landed in
        lines = fh.read().decode("utf-8", "replace").splitlines()
    for ln in lines:
        if '"validity_' in ln and '"event": "call"' in ln:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if str(r.get("tool", "")).startswith("validity_"):
                calls[r["id"]] = r
                wanted.add(r["id"])
    for ln in lines:
        if '"event": "outcome"' in ln:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if r.get("id") in wanted:
                outs[r["id"]] = r
    return calls, outs


def _worker_rows(conn, key):
    prefix = key + "__refuter_"
    return [dict(r) for r in conn.execute(
        "SELECT id, key, name, turn, role, text, extra, ts FROM fleet_turns "
        "WHERE key = ? OR substr(key, 1, ?) = ? ORDER BY ts, id",
        (key, len(prefix), prefix))]


def _call_text(call):
    args = {k: _arg_text(call.get("args"), k) for k in (call.get("args") or {})}
    return "%s %s" % (call.get("tool"), json.dumps(args, ensure_ascii=False, sort_keys=True))


def _claim_of_call(call):
    return _arg_text(call.get("args"), "config_id")


def sync_worker(worker_key, *, ledger_path=None, run_id="", name="", jid="", outcome=None,
                reviewed=None, tail_bytes=None, dry_run=False):
    """Append the audit rows for one worker conversation. Returns a stats dict.

    `outcome` = {"outcome":..., "status":..., "reason":...} (live) or None (the backfill passes the
    history entry). `reviewed` says the worker went through a review (so a missing reviewer text
    is stated as not_recorded rather than being silently absent); None = infer from the rows.
    Raises on a database failure: a ledger that did not land has to be knowable.
    """
    from bridge import session_store as S

    m = _KEY_RE.match(worker_key)
    run_id = run_id or (m.group("run") if m else "")
    name = name or (m.group("name") if m else "")
    conn = S._db(import_files=False)
    try:
        rows = _worker_rows(conn, worker_key)
    finally:
        conn.close()
    if not rows:
        return {"worker_key": worker_key, "audited": False, "reason": "no fleet_turns rows"}
    t0, t1 = rows[0]["ts"], rows[-1]["ts"]

    # ---- tool calls that belong to THIS worker: its name, the job id when both sides know it,
    # and the conversation's time window.
    calls, outs = _read_events(ledger_path, tail_bytes)
    mine = []
    for c in calls.values():
        if (c.get("worker") or "") != name:
            continue
        if jid and c.get("task") and c["task"] != jid:
            continue
        if not (t0 - 120 <= c["ts"] <= t1 + 900):
            continue
        mine.append(c)
    mine.sort(key=lambda c: c["ts"])

    goal = ""
    for r in rows:
        if r["role"] == "meta":
            try:
                goal = json.loads(r["extra"] or "{}").get("goal", "") or ""
            except ValueError:
                pass
            break

    # ---- the claim ids of the conversation
    claims = []
    for c in mine:
        cid = _claim_of_call(c)
        if cid and cid not in claims:
            claims.append(cid)
    if not claims:
        gm = _GOAL_CLAIM_RE.search(goal or "")
        if gm and (mine or "validity" in (goal or "").lower()):
            claims.append(gm.group(1))
    if not mine and not claims and "validity" not in (goal or "").lower():
        return {"worker_key": worker_key, "audited": False, "reason": "not a validity conversation"}
    if not claims:
        claims = [UNATTRIBUTED]

    # ---- items: (ts, order, role_tag, text, source_table, source_key, extra, claim or None)
    items = []
    has_refuter = False
    for r in rows:
        role = _ROLE_OF.get(r["role"])
        if not role:
            continue                                   # metric / guid / note: not conversation
        try:
            ex = json.loads(r["extra"] or "{}")
        except ValueError:
            ex = {}
        text = r["text"]
        if role == "goal":
            text = ex.get("goal", "") or ""
        if role.startswith("refuter") or role == "verdict":
            has_refuter = True
        keep = {k: ex[k] for k in ("route", "lens", "sha16", "pre_redaction_sha16", "truncated",
                                   "orig_chars", "preamble_id", "gpt_id", "refute_count",
                                   "round") if k in ex}
        keep["fleet_turns_key"] = r["key"]
        keep["turn"] = r["turn"]
        items.append((r["ts"], 0, role, text, "fleet_turns", str(r["id"]), keep, None))
    for c in mine:
        cid = _claim_of_call(c)
        items.append((c["ts"], 1, "tool_call", _call_text(c), "tool_events.jsonl",
                      "call:" + c["id"], {"tool": c.get("tool"), "ledger_id": c["id"],
                                          "task": c.get("task"), "attr": c.get("attr")},
                      cid or None))
        o = outs.get(c["id"])
        if o:
            res = o.get("result") or {}
            items.append((o["ts"], 2, "tool_result", str(res.get("text") or o.get("error") or ""),
                          "tool_events.jsonl", "outcome:" + c["id"],
                          {"tool": c.get("tool"), "ledger_id": c["id"], "ok": o.get("ok"),
                           "result_len": res.get("len"),
                           "cut_in_tool_ledger": bool(res.get("truncated")),
                           "error": o.get("error") or ""}, cid or None))
    if outcome:
        otext = "%s%s" % (outcome.get("outcome") or outcome.get("status") or "",
                          (": " + outcome["reason"]) if outcome.get("reason") else "")
        items.append((max(t1, mine[-1]["ts"] if mine else t1) + 0.001, 3, "outcome", otext, "worker_close", worker_key,
                      {"status": outcome.get("status"), "outcome": outcome.get("outcome")}, None))
    if (reviewed if reviewed is not None else False) and not has_refuter:
        # A review ran but its text was never recorded (before this ledger existed): say so.
        for tag in ("refuter_prompt", "refuter_reply"):
            items.append((t1, 4, tag, NOT_RECORDED, "not_recorded", worker_key,
                          {"not_recorded": True}, None))
    items.sort(key=lambda it: (it[0], it[1]))

    # ---- bind to claims: a tool call without a claim argument goes to the claim most recently
    # judged before it (else the first); everything else goes to every claim of the worker.
    out = []
    last_claim = claims[0]
    for seq, (ts, _o, role, text, st, sk, ex, cid) in enumerate(items):
        if cid:
            last_claim = cid
        targets = [cid] if cid else ([last_claim] if role in ("tool_call", "tool_result")
                                     else claims)
        for target in targets:
            out.append({"claim_id": target, "run_id": run_id, "worker_key": worker_key,
                        "seq": seq, "role_tag": role, "ts": ts, "text": text,
                        "source_table": st, "source_key": sk, "extra": ex})
    stats = {"worker_key": worker_key, "audited": True, "claims": claims, "rows": len(out)}
    if dry_run:
        stats["inserted"], stats["already_there"] = 0, 0
        stats["dry_run"] = True
        stats["roles"] = sorted({r["role_tag"] for r in out})
        return stats
    stats["inserted"], stats["already_there"] = S.append_validity_audit(out)
    return stats


def sync_unattributed_calls(ledger_path, since, until, *, tail_bytes=None, dry_run=False):
    """Validity tool calls that no worker conversation claimed (a person or an interactive
    agent called them): bound to their own claim id, or UNATTRIBUTED. Never dropped."""
    from bridge import session_store as S

    calls, outs = _read_events(ledger_path, tail_bytes)
    conn = S._db(import_files=False)
    try:
        have = {r[0] for r in conn.execute(
            "SELECT source_key FROM validity_audit WHERE source_table = 'tool_events.jsonl'")}
    finally:
        conn.close()
    out = []
    for c in sorted(calls.values(), key=lambda c: c["ts"]):
        if not (since <= c["ts"] <= until) or ("call:" + c["id"]) in have:
            continue
        cid = _claim_of_call(c) or UNATTRIBUTED
        ex = {"tool": c.get("tool"), "ledger_id": c["id"], "task": c.get("task"),
              "worker": c.get("worker"), "unattributed_to_a_worker": True}
        out.append({"claim_id": cid, "run_id": "", "worker_key": "", "seq": 0,
                    "role_tag": "tool_call", "ts": c["ts"], "text": _call_text(c),
                    "source_table": "tool_events.jsonl", "source_key": "call:" + c["id"],
                    "extra": ex})
        o = outs.get(c["id"])
        if o:
            res = o.get("result") or {}
            out.append({"claim_id": cid, "run_id": "", "worker_key": "", "seq": 1,
                        "role_tag": "tool_result", "ts": o["ts"],
                        "text": str(res.get("text") or o.get("error") or ""),
                        "source_table": "tool_events.jsonl", "source_key": "outcome:" + c["id"],
                        "extra": dict(ex, ok=o.get("ok"), result_len=res.get("len"),
                                      cut_in_tool_ledger=bool(res.get("truncated")))})
    if dry_run:
        return {"rows": len(out), "inserted": 0, "already_there": 0, "dry_run": True}
    ins, skipped = S.append_validity_audit(out) if out else (0, 0)
    return {"rows": len(out), "inserted": ins, "already_there": skipped}


def sync_worker_logged(worker_key, **kw):
    """The live hook's entry: never raises into a worker's close, never silent about failure."""
    try:
        return sync_worker(worker_key, **kw)
    except Exception as exc:
        try:
            sys.stderr.write("[validity_audit] sync of %s FAILED: %s: %s\n"
                             % (worker_key, type(exc).__name__, str(exc)[:200]))
            sys.stderr.flush()
        except Exception:
            pass
        return {"worker_key": worker_key, "audited": False,
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
