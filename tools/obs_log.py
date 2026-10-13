# -*- coding: utf-8 -*-
"""Small append-only JSONL logs for server-side evidence that no other ledger holds.

Two files live in the same state directory as tool_events.jsonl:

  mcp_inbound.jsonl           one line per MCP JSON-RPC request arrival (initialize, tools/list,
                              ping, ... and a bare arrival line for tools/call). Answers "did the
                              client ever connect" apart from "it connected but called no tool".
  fleet_submit_refusals.jsonl one line per refused fleet_submit, with the reason and a short head
                              of what was refused.

Rules, shared by both writers:
  * The directory is whatever tools.tool_ledger resolves for tool_events.jsonl at call time, so a
    test (and the repo-wide isolation fixture) that moves the ledger moves these too.
  * NEVER log arguments, bodies, tokens or passwords. Callers pass only short, named fields.
  * Failure to write never reaches the caller. The first failure per process is noted on stderr.
  * Size-capped: past MAX_BYTES the file is rewritten with only its tail (whole lines).
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

MAX_BYTES = 20 * 1024 * 1024

INBOUND_NAME = "mcp_inbound.jsonl"
REFUSALS_NAME = "fleet_submit_refusals.jsonl"

_LOCK = threading.Lock()
_WARNED = False

_ADDRESS_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def head(text, limit: int) -> str:
    """First `limit` characters with whitespace (newlines included) flattened to single spaces,
    and mail-address shapes removed."""
    flat = " ".join(str(text or "").split())
    flat = _ADDRESS_RE.sub("[address]", flat)
    return flat[:limit]


def _state_dir() -> str:
    from tools import tool_ledger
    return os.path.dirname(str(tool_ledger._repo_path()))


def _note_failure(exc: BaseException) -> None:
    global _WARNED
    if _WARNED:
        return
    _WARNED = True
    try:
        sys.stderr.write("obs_log: write failed (%s); further failures are silent\n"
                         % type(exc).__name__)
    except Exception:
        pass


def _trim_to_tail(path: str, max_bytes: int) -> None:
    """Keep roughly the newest half of the file, starting on a line boundary."""
    keep = max(1, max_bytes // 2)
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - keep))
        data = fh.read()
    nl = data.find(b"\n")
    data = data[nl + 1:] if nl >= 0 else b""
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def append(name: str, row: dict) -> None:
    """Append one JSON line to <state dir>/<name>. Never raises."""
    try:
        now = time.time()
        line = json.dumps(dict(ts=now, at=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
                               **row), ensure_ascii=False)
        path = os.path.join(_state_dir(), name)
        with _LOCK:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            try:
                if os.path.getsize(path) >= MAX_BYTES:
                    _trim_to_tail(path, MAX_BYTES)
            except FileNotFoundError:
                pass
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:
        _note_failure(exc)


# --- MCP inbound ---------------------------------------------------------------------------

#: Largest request body inspected for its method. A bigger one is logged as "(oversize)".
MAX_PEEK_BYTES = 1024 * 1024


def _session_label(raw: str) -> str:
    """Same hash tool_ledger.session_fingerprint uses, so the two files can be joined."""
    if not raw:
        return ""
    import hashlib
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def methods_from_body(body: bytes):
    """[(method, tool_or_empty)] for a JSON-RPC body (single or batch). Only the method and, for
    tools/call, the tool NAME are read; nothing else is returned."""
    try:
        doc = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return [("(unparsed)", "")]
    msgs = doc if isinstance(doc, list) else [doc]
    out = []
    for m in msgs[:50]:
        if not isinstance(m, dict):
            out.append(("(non-object)", ""))
            continue
        method = str(m.get("method") or "(response)")[:80]
        tool = ""
        if method == "tools/call":
            params = m.get("params")
            if isinstance(params, dict):
                tool = str(params.get("name") or "")[:80]
        out.append((method, tool))
    return out


def log_inbound(methods, session_id: str, client_ip: str, user_agent: str) -> None:
    """One line per (method, tool) pair. Never raises."""
    try:
        sid = _session_label(session_id)
        ua = str(user_agent or "")[:80]
        for method, tool in methods:
            row = {"method": method, "session": sid, "client_ip": client_ip or "", "ua": ua}
            if tool:
                row["tool"] = tool
            append(INBOUND_NAME, row)
    except Exception as exc:
        _note_failure(exc)


# --- fleet_submit refusals -----------------------------------------------------------------

def log_refusal(reason: str, goal_text: str, source: str, similar_to: str = "") -> None:
    """One line for a refused fleet_submit. Never raises."""
    try:
        row = {"reason": reason, "goal_len": len(str(goal_text or "")),
               "goal_head": head(goal_text, 80), "source": head(source, 80)}
        if similar_to:
            row["similar_to"] = str(similar_to)
        append(REFUSALS_NAME, row)
    except Exception as exc:
        _note_failure(exc)
