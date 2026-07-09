"""session_store.py -- durable local session registry for the interactive bridge loop.

M365 Copilot keeps conversation context server-side, so "resume" really means
reattaching to a conversation URL. This module is the missing local piece: a
small, pure-stdlib JSON-file registry that tracks known sessions, their
Copilot conversation URL, a turn transcript, and a pending-input queue.

No third-party deps. No threads, no locks, no background loop -- just atomic
file writes via os.replace(). Concurrency target is modest: single machine,
occasional concurrent reader/writer.

Transcript files (<sid>.jsonl under SESS_DIR) follow the same shape used by
fleet transcripts (.fleet/transcripts/*.jsonl):
  line 1:  {"meta": true, "sid": ..., "title": ..., "ts": ...}
  line N:  {"turn": <int>, "role": "user"|"assistant", "text": ..., "ts": ...}
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SESS_DIR = os.path.join(str(REPO), ".fleet", "sessions")
SID_RE = re.compile(r"^s\d{10}[0-9a-f]{4}$")


def _base_dir():
    """Indirection point so tests can monkeypatch the storage location."""
    return SESS_DIR


def _ensure_dir():
    d = _base_dir()
    os.makedirs(d, exist_ok=True)
    return d


def _valid_sid(sid):
    return isinstance(sid, str) and bool(SID_RE.fullmatch(sid))


def _sid_filename(sid, suffix):
    if not _valid_sid(sid):
        raise ValueError("invalid session id")
    safe = hashlib.sha256(sid.encode("ascii")).hexdigest()
    return safe + suffix


def _session_file(sid, suffix):
    base = Path(_base_dir()).resolve()
    path = (base / _sid_filename(sid, suffix)).resolve()
    if path.parent != base:
        raise ValueError("session path escaped base directory")
    return str(path)


def _transcript_ref(sid):
    return "sessions/" + _sid_filename(sid, ".jsonl")


def _sess_path(sid):
    return _session_file(sid, ".json")


def _transcript_path(sid):
    return _session_file(sid, ".jsonl")


def _atomic_write_text(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _atomic_write_json(path, obj):
    _atomic_write_text(path, json.dumps(obj, ensure_ascii=False))


def _read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def new_session(title=""):
    """Create a new session, persist it, and return the session dict."""
    _ensure_dir()
    sid = "s" + time.strftime("%m%d%H%M%S", time.gmtime()) + "%04x" % random.getrandbits(16)
    now = time.time()
    sess = {
        "sid": sid,
        "title": title or "",
        "conv_url": "",
        "created_ts": now,
        "last_active_ts": now,
        "status": "active",
        "turns": 0,
        "transcript": _transcript_ref(sid),
        "pending": [],
    }
    _atomic_write_json(_sess_path(sid), sess)
    return sess


def load(sid):
    """Load a session dict by sid, or None if missing/corrupt."""
    if not _valid_sid(sid):
        return None
    path = _sess_path(sid)
    if not os.path.isfile(path):
        return None
    try:
        return _read_json(path)
    except (ValueError, OSError):
        return None


def list_sessions():
    """Return all sessions, newest-first by last_active_ts. Skips corrupt files."""
    d = _base_dir()
    if not os.path.isdir(d):
        return []
    sessions = []
    for name in os.listdir(d):
        if not name.endswith(".json"):
            continue
        path = os.path.join(d, name)
        try:
            sess = _read_json(path)
        except (ValueError, OSError):
            continue
        if not isinstance(sess, dict) or "sid" not in sess:
            continue
        if not _valid_sid(sess.get("sid")):
            continue
        sessions.append(sess)
    sessions.sort(key=lambda s: s.get("last_active_ts", 0), reverse=True)
    return sessions


def touch(sid, **fields):
    """Merge fields into the session, bump last_active_ts, atomic rewrite."""
    if not _valid_sid(sid):
        raise ValueError("invalid session id")
    sess = load(sid)
    if sess is None:
        sess = {
            "sid": sid,
            "title": "",
            "conv_url": "",
            "created_ts": time.time(),
            "last_active_ts": time.time(),
            "status": "active",
            "turns": 0,
            "transcript": _transcript_ref(sid),
            "pending": [],
        }
    sess.update(fields)
    sess["last_active_ts"] = time.time()
    _ensure_dir()
    _atomic_write_json(_sess_path(sid), sess)
    return sess


def append_turn(sid, role, text):
    """Append one turn line to <sid>.jsonl, emitting the meta header on first use."""
    if not _valid_sid(sid):
        raise ValueError("invalid session id")
    _ensure_dir()
    path = _transcript_path(sid)
    is_new = not os.path.isfile(path)
    sess = load(sid)
    title = sess.get("title", "") if sess else ""
    turn_num = (sess.get("turns", 0) if sess else 0) + 1

    lines = []
    if is_new:
        meta = {"meta": True, "sid": sid, "title": title, "ts": time.time()}
        lines.append(json.dumps(meta, ensure_ascii=False))
    turn_rec = {"turn": turn_num, "role": role, "text": text, "ts": time.time()}
    lines.append(json.dumps(turn_rec, ensure_ascii=False))

    with open(path, "a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")

    touch(sid, turns=turn_num)


def queue_input(sid, text):
    """Append text to the session's pending list (atomic read-modify-write)."""
    if not _valid_sid(sid):
        raise ValueError("invalid session id")
    sess = load(sid)
    if sess is None:
        sess = touch(sid)
    pending = list(sess.get("pending", []))
    pending.append(text)
    touch(sid, pending=pending)


def pop_input(sid):
    """Pop the first pending entry (FIFO). Returns None if empty."""
    if not _valid_sid(sid):
        return None
    sess = load(sid)
    if sess is None:
        return None
    pending = list(sess.get("pending", []))
    if not pending:
        return None
    first = pending.pop(0)
    touch(sid, pending=pending)
    return first


def latest_active():
    """Most recent session (by last_active_ts) that has a non-empty conv_url."""
    for sess in list_sessions():
        if sess.get("conv_url"):
            return sess
    return None
