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
#: Where content-similarity is NOTED without refusing anything. The owner's instruction
#: (2026-10-26/27) is that a goal must never be stopped because its wording resembles another
#: goal's -- normalisation/similarity may run, but only to RECORD, never to block. A separate
#: file from REFUSALS_NAME on purpose: a row here is an observation, not a refusal, and must not
#: be miscounted as one.
SIMILAR_OBSERVED_NAME = "fleet_submit_similarity.jsonl"

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


def id_label(raw) -> str:
    """Non-reversible 16-hex label of an identifier ("" for empty). The one hash every writer and
    reader of attribution uses (session ids, conversation ids, trace ids), so they can be joined
    without a raw identifier ever reaching a log."""
    return _session_label(str(raw or ""))


# --- header NAMES and identifier-shaped header values ---------------------------------------

#: Header names that may carry a correlation / conversation / trace identifier. Only a hash of
#: the value of such a header is ever kept. Everything else is recorded by NAME alone.
_ID_HEADER_RE = re.compile(
    r"^(traceparent|tracestate|request-id|x-request-id|x-correlation-id|x-correlationid"
    r"|x-ms-[a-z0-9-]*(id|trace|correlation|conversation|session|activity)[a-z0-9-]*"
    r"|x-[a-z0-9-]*(conversation|session|trace|correlation)[a-z0-9-]*)$")
#: ... unless the name also looks like a credential: those are never read at all.
_SECRET_HEADER_RE = re.compile(r"auth|cookie|token|key|secret|passw|bearer|sig|credential|api")
#: Header carrying the MCP session itself: hashed into `session`, not repeated here.
_SESSION_HEADER = "mcp-session-id"
_MAX_HDR_NAMES = 40
_MAX_ID_HEADERS = 8


def header_info(headers):
    """(names, ids) from raw ASGI headers [(bytes, bytes)]: `names` the sorted lower-case header
    NAMES (no values); `ids` {name: id_label(value)} only for identifier-shaped, non-credential
    names. Never raises."""
    names, ids = set(), {}
    try:
        for hk, hv in (headers or []):
            k = hk.decode("latin-1").lower().strip()
            if not k:
                continue
            names.add(k)
            if (k != _SESSION_HEADER and len(ids) < _MAX_ID_HEADERS and _ID_HEADER_RE.match(k)
                    and not _SECRET_HEADER_RE.search(k)):
                v = hv.decode("latin-1").strip()
                if v:
                    ids[k] = id_label(v)
    except Exception:
        pass
    return sorted(names)[:_MAX_HDR_NAMES], ids


#: A parameter / _meta key whose VALUE is hashed (never stored) because it looks like an id.
_ID_KEY_RE = re.compile(r"(conversation|session|trace|correlation|activity|request|thread|chat)"
                        r"[-_a-z0-9]*(id)?$|^id$|[-_]id$", re.I)


def _id_hashes(mapping) -> dict:
    out = {}
    if isinstance(mapping, dict):
        for key, val in list(mapping.items())[:60]:
            k = str(key)[:60]
            if isinstance(val, (str, int)) and not isinstance(val, bool) and _ID_KEY_RE.search(k) \
                    and not _SECRET_HEADER_RE.search(k.lower()):
                out[k] = id_label(val)
                if len(out) >= _MAX_ID_HEADERS:
                    break
    return out


def extras_from_body(body: bytes):
    """One dict per JSON-RPC message, aligned with methods_from_body: only KEY NAMES and
    hashes of id-shaped values. For `initialize`: the clientInfo and capability key
    names, the key names of params and params._meta, and hashes of id-shaped values in them.
    For other methods: the key names of params._meta and hashes of id-shaped values there.
    Arguments are never read. Never raises."""
    try:
        doc = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return [{}]
    msgs = doc if isinstance(doc, list) else [doc]
    out = []
    for m in msgs[:50]:
        ex = {}
        try:
            params = m.get("params") if isinstance(m, dict) else None
            if isinstance(params, dict):
                meta = params.get("_meta")
                if isinstance(meta, dict):
                    ex["meta_keys"] = sorted(str(k)[:60] for k in meta)[:30]
                    ids = _id_hashes(meta)
                    if ids:
                        ex["meta_ids"] = ids
                if m.get("method") == "initialize":
                    ex["param_keys"] = sorted(str(k)[:60] for k in params)[:30]
                    ci = params.get("clientInfo")
                    if isinstance(ci, dict):
                        ex["client_keys"] = sorted(str(k)[:40] for k in ci)[:20]
                        ids = _id_hashes(ci)
                        if ids:
                            ex["client_ids"] = ids
                    caps = params.get("capabilities")
                    if isinstance(caps, dict):
                        ex["cap_keys"] = sorted(str(k)[:40] for k in caps)[:30]
                    ids = _id_hashes(params)
                    if ids:
                        ex["param_ids"] = ids
        except Exception:
            ex = {}
        out.append(ex)
    return out


_SEEN_HDR_SETS = set()
_SEEN_HDR_SETS_MAX = 64


def log_inbound(methods, session_id: str, client_ip: str, user_agent: str,
                headers=None, extras=None) -> None:
    """One line per (method, tool) pair. Never raises.

    `headers` is the raw ASGI header list; only header NAMES go to the log (`hdr`, written on
    every `initialize`, and whenever a header-name set not yet seen by this process appears), plus
    `hdr_ids` = hashes of identifier-shaped, non-credential header values. `extras` is the list
    from extras_from_body (key names and hashes only)."""
    try:
        sid = _session_label(session_id)
        ua = str(user_agent or "")[:80]
        names, ids = header_info(headers) if headers else ([], {})
        key = tuple(names)
        new_set = False
        if names:
            with _LOCK:
                if key not in _SEEN_HDR_SETS:
                    if len(_SEEN_HDR_SETS) >= _SEEN_HDR_SETS_MAX:
                        _SEEN_HDR_SETS.clear()
                    _SEEN_HDR_SETS.add(key)
                    new_set = True
        for i, (method, tool) in enumerate(methods):
            row = {"method": method, "session": sid, "client_ip": client_ip or "", "ua": ua}
            if tool:
                row["tool"] = tool
            if names and (new_set or method == "initialize"):
                row["hdr"] = names
            if ids:
                row["hdr_ids"] = ids
            ex = extras[i] if extras and i < len(extras) else None
            if ex:
                row.update(ex)
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


def log_similarity_observed(goal_text: str, source: str, similar_to: str = "",
                            similarity: float = 0.0) -> None:
    """Record that an incoming goal RESEMBLED one already in flight -- WITHOUT refusing it.

    This is the 'normalise in the background' half of the owner's instruction: the goal is
    queued normally, and the resemblance is noted here only so the pattern stays visible. It
    is written to SIMILAR_OBSERVED_NAME, never REFUSALS_NAME, because nothing was refused.
    Never raises.
    """
    try:
        row = {"goal_len": len(str(goal_text or "")),
               "goal_head": head(goal_text, 80), "source": head(source, 80),
               "queued": True}
        if similar_to:
            row["similar_to"] = str(similar_to)
        if similarity:
            row["similarity"] = round(float(similarity), 4)
        append(SIMILAR_OBSERVED_NAME, row)
    except Exception as exc:
        _note_failure(exc)
