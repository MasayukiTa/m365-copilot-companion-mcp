# -*- coding: utf-8 -*-
"""Which fleet worker / conversation does an MCP request belong to, and which MCP tool calls
were in flight during a socket turn.

THE GAP (measured 2026-10-15). The server side of a Copilot Studio conversation is an MCP
SESSION (the `Mcp-Session-Id` the server mints in its answer to `initialize`). Nothing on that
side names the fleet worker: `tool_events.jsonl` had a worker only when a coordinator turn window
matched unambiguously (6 % of 4,608 calls) and `mcp_inbound.jsonl` had none, so "is a turn that
shows 60 s of update frames just waiting for a long server-side tool call?" could not be asked.

WHAT IS PROVEN (see the commit message / report): over 5,004 inbound rows, 290 `initialize`
requests produced exactly 290 sessions (one new session ~0.25 s after each, max 4.4 s) and every
session's first row is the `notifications/initialized` that follows. A session is therefore one
Copilot conversation's MCP connection (it carries tools/list and a median of 11 tools/call, up to
128 over up to 100 minutes).

THE JOIN. relay.mcp_ready.InitDetector pairs each fresh conversation's first send with the
`initialize` it caused (by an identifier when an inbound row carries one, else by time, counting
each initialize once) and now also knows the session that initialize created. The fleet worker
writes one `mcp_session_bound` event (worker, conversation, mcp_session, turn_at_bind, how,
candidates) to socket_route.jsonl. Everything else is a pure function of those events and
tool_events.jsonl:

  session_worker_map(route_rows)       mcp session -> binding
  turn_tools_fields(...)               the MCP calls in flight during one socket turn

`how` says how far to trust a binding: "id" exact; "unique" temporal and nothing else was
pending; "fifo" temporal with `candidates` sends pending (pairing may be crossed inside a
burst: such a binding is a hypothesis; check it with the calls' own timing).

Nothing here reads a request body, an argument or a credential; only ids and timestamps.
"""
from __future__ import annotations

import json
import os
import threading
import time

BOUND_EVENT = "mcp_session_bound"
TURN_TOOLS_EVENT = "socket_turn_tools"

#: A call with no outcome older than this at the end of a window is treated as abandoned, not in flight.
MAX_OPEN_S = 3600.0
#: How much of the tool ledger tail is read per turn (rows are large; ~8 MB covers hours).
LEDGER_TAIL_BYTES = 8 * 1024 * 1024
#: How much of socket_route.jsonl is read to rebuild bindings after a restart.
ROUTE_TAIL_BYTES = 4 * 1024 * 1024
#: Calls listed per record (the rest are only counted).
MAX_CALLS_LISTED = 25
_REGISTRY_MAX = 4096


# --- reading --------------------------------------------------------------------------------
def read_jsonl_tail(path, max_bytes):
    """([rows], first_ts) for the last `max_bytes` of a JSONL file; a partial first line is
    dropped. first_ts is the ts of the oldest row read (None when empty). Never raises."""
    rows = []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            start = max(0, size - int(max_bytes))
            fh.seek(start)
            data = fh.read()
        if start:
            nl = data.find(b"\n")
            data = data[nl + 1:] if nl >= 0 else b""
        for raw in data.split(b"\n"):
            if not raw.strip():
                continue
            try:
                rows.append(json.loads(raw.decode("utf-8", "replace")))
            except ValueError:
                continue
    except Exception:
        return rows, None
    first = None
    for r in rows:
        try:
            first = float(r["ts"])
            break
        except Exception:
            continue
    return rows, first


# --- bindings -------------------------------------------------------------------------------
def bound_fields(worker, conversation, turn_at_bind, binding, server_conversation=""):
    """Fields of a `mcp_session_bound` event from InitDetector.binding(). None if unbound."""
    if not binding:
        return None
    return {"mcp_session": str(binding.get("session") or ""), "worker": str(worker or ""),
            "conversation": str(conversation or ""),
            "server_conversation": str(server_conversation or ""),
            "turn_at_bind": int(turn_at_bind or 0), "how": str(binding.get("how") or ""),
            "candidates": int(binding.get("candidates") or 0),
            "sent_at": binding.get("sent_at"), "init_ts": binding.get("init_ts")}


def session_worker_map(route_rows):
    """{mcp_session: binding row}: the latest `mcp_session_bound` per session. Rows with an
    empty session (the birth row had not been read yet) are skipped."""
    out = {}
    for r in route_rows or []:
        if r.get("event") != BOUND_EVENT:
            continue
        s = str(r.get("mcp_session") or "")
        if s:
            out[s] = r
    return out


class Registry:
    """In-process view of the bindings; rebuilt from socket_route.jsonl after a restart."""

    def __init__(self, route_path_fn=None):
        self._lock = threading.Lock()
        self._by_session = {}
        self._loaded = False
        self._path_fn = route_path_fn

    def add(self, rec):
        s = str(rec.get("mcp_session") or "")
        if not s:
            return
        with self._lock:
            if len(self._by_session) >= _REGISTRY_MAX:
                for k in sorted(self._by_session, key=lambda k: self._by_session[k].get("ts", 0))[
                        :_REGISTRY_MAX // 4]:
                    self._by_session.pop(k, None)
            self._by_session[s] = dict(rec)

    def ensure_loaded(self):
        if self._loaded or not self._path_fn:
            return
        self._loaded = True
        try:
            rows, _ = read_jsonl_tail(self._path_fn(), ROUTE_TAIL_BYTES)
            for s, r in session_worker_map(rows).items():
                with self._lock:
                    self._by_session.setdefault(s, r)
        except Exception:
            pass

    def snapshot(self):
        self.ensure_loaded()
        with self._lock:
            return dict(self._by_session)


# --- tool calls -----------------------------------------------------------------------------
def tool_spans(tool_rows):
    """[{id, tool, session, start, end, ok}] from tool_events rows: a `call` row opens a span, the
    `outcome` row with the same id closes it (end None = no outcome seen)."""
    spans, by_id = [], {}
    for r in tool_rows or []:
        ev = r.get("event")
        try:
            if ev == "call":
                sp = {"id": str(r.get("id") or ""), "tool": str(r.get("tool") or "")[:80],
                      "session": str(r.get("session") or ""), "start": float(r["ts"]),
                      "end": None, "ok": None, "worker_logged": str(r.get("worker") or ""),
                      "timeout_s": _arg_timeout_s(r.get("args"))}
                spans.append(sp)
                if sp["id"]:
                    by_id[sp["id"]] = sp
            elif ev == "outcome":
                sp = by_id.get(str(r.get("id") or ""))
                if sp is not None:
                    sp["end"] = float(r["ts"])
                    sp["ok"] = r.get("ok")
        except Exception:
            continue
    return spans


def sessions_of_worker(worker, conversation, bindings, t1):
    """The MCP sessions bound to this worker's conversation (a set of session ids)."""
    mine = {s for s, b in bindings.items()
            if b.get("worker") == worker and (not conversation or b.get("conversation") == conversation)}
    if not mine:
        # The conversation id may not match (rehydrated / unknown): fall back to this worker's
        # most recent binding at or before the window end.
        cand = [b for b in bindings.values() if b.get("worker") == worker
                and float(b.get("ts", 0) or 0) <= t1 + 1.0]
        if cand:
            last = max(cand, key=lambda b: float(b.get("ts", 0) or 0))
            mine = {s for s, b in bindings.items() if b is last or (
                b.get("worker") == worker and b.get("conversation") == last.get("conversation"))}
    return mine


# --- is the worker's conversation waiting on a tool call of ours? ------------------------------
#: The server-side timeout each tool applies when the caller passes none (the `timeout`
#: parameter defaults in tools/code_exec.py, tools/jobs.py, tools/verify_ops.py).
TOOL_DEFAULT_TIMEOUT_S = {"run_python": 60.0, "shell_exec": 30.0, "job_wait": 90.0,
                          "verify_python": 60.0, "render_mermaid_png": 30.0}
#: A tool with no timeout parameter of its own: how long an open call still counts as work.
UNKNOWN_TOOL_TIMEOUT_S = 300.0
#: Slack on top of a tool's own timeout (process-tree kill, result write-back, ledger lag).
TIMEOUT_GRACE_S = 30.0
#: A `fifo` binding is only a hypothesis: its init must have followed its send by about this much.
FIFO_MAX_INIT_LAG_S = 10.0
#: Trust levels whose calls count as this worker's.
TRUSTED_HOW = ("id", "unique")
#: Ledger tail read for the in-flight check (open calls are recent; minutes of rows).
INFLIGHT_TAIL_BYTES = 2 * 1024 * 1024
_INFLIGHT_CACHE_S = 2.0
_inflight_cache = {"path": None, "at": 0.0, "spans": []}
_inflight_lock = threading.Lock()


def _arg_timeout_s(args):
    """The `timeout` argument of a ledger `call` row (top level or under `arguments`), or None."""
    try:
        if not isinstance(args, dict):
            return None
        for src in (args, args.get("arguments") if isinstance(args.get("arguments"), dict) else {}):
            for k in ("timeout", "timeout_s"):
                v = src.get(k)
                if isinstance(v, dict):
                    v = v.get("text")
                if v is not None:
                    f = float(v)
                    if f > 0:
                        return f
    except Exception:
        pass
    return None


def call_allowed_s(sp):
    """How long a call of this tool may stay open and still count as work in flight."""
    own = sp.get("timeout_s")
    if own is None:
        own = TOOL_DEFAULT_TIMEOUT_S.get(str(sp.get("tool") or "").split(".")[0], UNKNOWN_TOOL_TIMEOUT_S)
    return float(own) + TIMEOUT_GRACE_S


def _fifo_consistent(binding, sp, t0):
    """A fifo binding may be crossed with another worker's. Accept a call only if its timing fits:
    the call started in this turn (not before it) and the session's init followed the send."""
    if sp["start"] < t0 - 1.0:
        return False
    sent, init = binding.get("sent_at"), binding.get("init_ts")
    try:
        if sent is not None and init is not None and not 0.0 <= float(init) - float(sent) <= FIFO_MAX_INIT_LAG_S:
            return False
        if init is not None and sp["start"] < float(init) - 1.0:
            return False
    except (TypeError, ValueError):
        return False
    return True


def attributed_tool_idle_s(worker, conversation, t0, now, spans, bindings):
    """Seconds since the last tool activity attributed to this worker's turn, or None.

    Activity is a call still open (age 0) or one that ended in this turn (age since its end).
    The caller decides how long a silence after the last activity is tolerated (the model's
    own latency between tool calls: measured max 176 s over 881 gaps of completed turns).
    Counted: calls on sessions bound to the worker by `id` or `unique`; `fifo` ones only when
    `_fifo_consistent`; an open call older than `call_allowed_s` (its tool's own timeout plus
    grace) is a hung call and is NOT counted. Anything else, and an unattributed worker, gives
    None, so the caller behaves exactly as before."""
    mine = sessions_of_worker(worker, conversation, bindings, now)
    if not mine:
        return None
    last = None
    hung_start = None
    for sp in spans:
        if sp["session"] not in mine or sp["start"] > now:
            continue
        b = bindings[sp["session"]]
        how = str(b.get("how", ""))
        if how == "fifo":
            if not _fifo_consistent(b, sp, t0):
                continue
        elif how not in TRUSTED_HOW:
            continue
        if sp["end"] is None:
            if now - sp["start"] > call_allowed_s(sp):
                hung_start = sp["start"] if hung_start is None else max(hung_start, sp["start"])
                continue
            act = now
        else:
            if sp["end"] < t0:
                continue
            act = min(sp["end"], now)
        last = act if last is None else max(last, act)
    # A call open past its own timeout is hung. Activity from BEFORE it started cannot excuse
    # it (that would give a hung call the whole inter-call allowance); activity after it
    # started shows the turn moved on, so the call is not what the turn is waiting on.
    if hung_start is not None and (last is None or last < hung_start):
        return None
    return None if last is None else max(0.0, now - last)


def recent_spans(ledger_path, now=None):
    """Spans of the ledger tail, cached for a couple of seconds (the fleet polls every worker
    about once a second). Never raises."""
    try:
        now = time.time() if now is None else now
        with _inflight_lock:
            c = _inflight_cache
            if c["path"] == ledger_path and abs(now - c["at"]) < _INFLIGHT_CACHE_S:
                return c["spans"]
        rows, _ = read_jsonl_tail(ledger_path, INFLIGHT_TAIL_BYTES)
        spans = tool_spans(rows)
        with _inflight_lock:
            _inflight_cache.update(path=ledger_path, at=now, spans=spans)
        return spans
    except Exception:
        return []


def _union_s(intervals):
    total, cur_s, cur_e = 0.0, None, None
    for s, e in sorted(intervals):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += cur_e - cur_s
    return total


def turn_tools_fields(worker, turn, conversation, t0, t1, spans, bindings, ledger_first_ts=None):
    """Fields of a `socket_turn_tools` event: the MCP tool calls in flight at any point of the
    turn window [t0, t1] from the sessions bound to this worker's conversation.

    A call is in flight when start <= t1 and (no outcome yet, or outcome >= t0). Times in the
    listed calls are seconds relative to t0 (negative = started before the turn). `busy_s` is the
    union of the clipped intervals, `open_at_end` the calls still without an outcome at t1.
    `other_overlap` counts overlapping calls on sessions bound to ANOTHER worker and
    `unbound_overlap` those on sessions bound to nobody: nonzero unbound_overlap means the
    picture is incomplete."""
    mine = sessions_of_worker(worker, conversation, bindings, t1)
    listed, clipped, n_mine, other, unbound, open_end, longest = [], [], 0, 0, 0, 0, 0.0
    for sp in spans:
        if sp["start"] > t1:
            continue
        end = sp["end"]
        if end is None:
            if t1 - sp["start"] > MAX_OPEN_S:
                continue
        elif end < t0:
            continue
        sess = sp["session"]
        if sess in mine:
            n_mine += 1
            e = t1 if end is None else min(end, t1)
            clipped.append((max(sp["start"], t0), max(e, max(sp["start"], t0))))
            dur = (end if end is not None else t1) - sp["start"]
            longest = max(longest, dur)
            if end is None or end > t1:
                open_end += 1
            if len(listed) < MAX_CALLS_LISTED:
                listed.append({"tool": sp["tool"], "start_s": round(sp["start"] - t0, 2),
                               "end_s": None if end is None else round(end - t0, 2),
                               "dur_s": round(dur, 2), "ok": sp["ok"]})
        elif sess and sess in bindings:
            other += 1
        else:
            unbound += 1
    how = sorted({str(bindings[s].get("how", "")) for s in mine})
    return {"worker": str(worker or ""), "turn": turn, "conversation": str(conversation or ""),
            "t0": round(t0, 3), "t1": round(t1, 3), "window_s": round(t1 - t0, 2),
            "mcp_sessions": sorted(mine), "bind_how": how,
            "n_calls": n_mine, "busy_s": round(_union_s(clipped), 2),
            "longest_call_s": round(longest, 2), "open_at_end": open_end, "calls": listed,
            "other_overlap": other, "unbound_overlap": unbound,
            "ledger_covers_window": bool(ledger_first_ts is not None and ledger_first_ts <= t0)}


def turn_tools_for(worker, turn, conversation, t0, t1, ledger_path, bindings):
    """Read the ledger tail and build the fields. Never raises (returns None on failure)."""
    try:
        rows, first = read_jsonl_tail(ledger_path, LEDGER_TAIL_BYTES)
        return turn_tools_fields(worker, turn, conversation, t0, t1, tool_spans(rows), bindings, first)
    except Exception:
        return None
