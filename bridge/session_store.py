"""session_store.py -- durable local session registry for the interactive bridge loop.

M365 Copilot keeps conversation context server-side, so "resume" really means
reattaching to a conversation URL. This module is the missing local piece: it
tracks known sessions, their Copilot conversation URL, a turn transcript, and a
pending-input queue.

SQLITE, NOT A PILE OF FILES, AND THE REASON IS THE TAB. Two complaints share one
cause. Conversations lose their history, and browser tabs grow heavy the longer a
conversation runs -- measured at roughly 2.66 MB per second that a renderer stays
at work. Both are fixed by being able to RECYCLE the conversation freely, and
recycling is only free once the history lives somewhere that survives it. A
JSONL file per session could hold the text, but answering "give me the last N
turns" meant reading a whole growing file, so nothing was built on it. An indexed
store makes bounded reads cheap, which is what lets the bridge start a fresh
conversation whenever it likes and re-supply only what is needed.

To be exact about what this does and does not buy, since the two get conflated:
storing turns here does not make a tab lighter by itself. A whole conversation
was measured at 1.9 MB against a tab costing hundreds. What makes the tab light
is not keeping one -- and what makes discarding one safe is this store.

THE JSONL FILES ARE STILL WRITTEN, and they are no longer the source of truth.
The fleet cockpit reads `.fleet/conversations.json`, whose rows point at
`sessions/<hash>.jsonl` in the same line shape as fleet transcripts. Dropping
them would break a reader nobody would remember to look at. They are appended
alongside each insert, so they cannot drift from the table.

EXISTING SESSIONS ARE IMPORTED, NOT ABANDONED. The complaint being fixed here is
that history disappears; a migration that left 500 sessions behind would be that
same complaint with a new cause. Import runs once per process per directory, skips
what is already in the table, and tolerates a corrupt file by leaving it out
rather than failing the read.

Transcript files (<hash>.jsonl under SESS_DIR) keep the shape used by fleet
transcripts (.fleet/transcripts/*.jsonl):
  line 1:  {"meta": true, "sid": ..., "title": ..., "ts": ...}
  line N:  {"turn": <int>, "role": "user"|"assistant", "text": ..., "ts": ...}
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sqlite3
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SESS_DIR = os.path.join(str(REPO), ".fleet", "sessions")
SID_RE = re.compile(r"^s\d{10}[0-9a-f]{4}$")

#: Directories whose file-backed sessions have been imported in THIS process. Keyed by path
#: rather than a single flag because tests point `_base_dir` at a fresh temp directory.
_IMPORTED = set()


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


def _db_path():
    return os.path.join(_base_dir(), "sessions.sqlite3")


def _connect():
    conn = sqlite3.connect(_db_path(), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL so a reader (the cockpit, a CLI) never blocks the bridge mid-turn.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _initialize(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            sid            TEXT PRIMARY KEY,
            title          TEXT NOT NULL DEFAULT '',
            conv_url       TEXT NOT NULL DEFAULT '',
            created_ts     REAL NOT NULL,
            last_active_ts REAL NOT NULL,
            status         TEXT NOT NULL DEFAULT 'active',
            turns          INTEGER NOT NULL DEFAULT 0,
            transcript     TEXT NOT NULL DEFAULT '',
            pending_json   TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS turns (
            sid   TEXT NOT NULL,
            turn  INTEGER NOT NULL,
            role  TEXT NOT NULL,
            text  TEXT NOT NULL,
            ts    REAL NOT NULL,
            PRIMARY KEY (sid, turn),
            FOREIGN KEY (sid) REFERENCES sessions(sid) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS sessions_active_idx ON sessions(last_active_ts DESC);
        """
    )


def _db():
    """A connection to an initialized, imported store."""
    _ensure_dir()
    conn = _connect()
    _initialize(conn)
    _import_files_once(conn)
    return conn


def _read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _import_files_once(conn):
    """Bring any file-backed sessions into the table. Idempotent, once per directory."""
    base = _base_dir()
    key = os.path.abspath(base)
    if key in _IMPORTED:
        return
    _IMPORTED.add(key)
    try:
        names = [n for n in os.listdir(base) if n.endswith(".json")]
    except OSError:
        return
    have = {r["sid"] for r in conn.execute("SELECT sid FROM sessions")}
    for name in names:
        try:
            sess = _read_json(os.path.join(base, name))
        except (ValueError, OSError):
            # A file we cannot parse is left where it is. Failing the whole import over one
            # bad file would hide every good session behind it.
            continue
        if not isinstance(sess, dict):
            continue
        sid = sess.get("sid")
        if not _valid_sid(sid) or sid in have:
            continue
        _write_session(conn, {
            "sid": sid,
            "title": sess.get("title", "") or "",
            "conv_url": sess.get("conv_url", "") or "",
            "created_ts": float(sess.get("created_ts") or time.time()),
            "last_active_ts": float(sess.get("last_active_ts") or 0.0),
            "status": sess.get("status", "active") or "active",
            "turns": int(sess.get("turns") or 0),
            "transcript": sess.get("transcript", "") or _transcript_ref(sid),
            "pending": list(sess.get("pending") or []),
        })
        have.add(sid)
        _import_transcript(conn, sid, os.path.join(base, name[: -len(".json")] + ".jsonl"))


def _import_transcript(conn, sid, path):
    """Copy one <hash>.jsonl into the turns table. Missing or partial files are fine."""
    if not os.path.isfile(path):
        return
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue          # a torn last line loses that line, not the transcript
                if not isinstance(rec, dict) or rec.get("meta"):
                    continue
                if rec.get("turn") is None or rec.get("role") is None:
                    continue
                rows.append((sid, int(rec["turn"]), str(rec.get("role")),
                             str(rec.get("text") or ""), float(rec.get("ts") or 0.0)))
    except OSError:
        return
    if rows:
        conn.executemany(
            "INSERT OR IGNORE INTO turns (sid, turn, role, text, ts) VALUES (?, ?, ?, ?, ?)",
            rows)


def _row_to_session(row):
    return {
        "sid": row["sid"],
        "title": row["title"],
        "conv_url": row["conv_url"],
        "created_ts": row["created_ts"],
        "last_active_ts": row["last_active_ts"],
        "status": row["status"],
        "turns": row["turns"],
        "transcript": row["transcript"],
        "pending": json.loads(row["pending_json"] or "[]"),
    }


def _write_session(conn, sess):
    conn.execute(
        """INSERT INTO sessions
               (sid, title, conv_url, created_ts, last_active_ts, status, turns,
                transcript, pending_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(sid) DO UPDATE SET
               title=excluded.title, conv_url=excluded.conv_url,
               created_ts=excluded.created_ts, last_active_ts=excluded.last_active_ts,
               status=excluded.status, turns=excluded.turns,
               transcript=excluded.transcript, pending_json=excluded.pending_json""",
        (sess["sid"], sess.get("title", ""), sess.get("conv_url", ""),
         float(sess.get("created_ts") or time.time()),
         float(sess.get("last_active_ts") or time.time()),
         sess.get("status", "active"), int(sess.get("turns") or 0),
         sess.get("transcript", "") or _transcript_ref(sess["sid"]),
         json.dumps(list(sess.get("pending") or []), ensure_ascii=False)))


def new_session(title=""):
    """Create a new session, persist it, and return the session dict."""
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
    conn = _db()
    try:
        _write_session(conn, sess)
    finally:
        conn.close()
    return sess


def load(sid):
    """Load a session dict by sid, or None if missing."""
    if not _valid_sid(sid):
        return None
    conn = _db()
    try:
        row = conn.execute("SELECT * FROM sessions WHERE sid = ?", (sid,)).fetchone()
    finally:
        conn.close()
    return _row_to_session(row) if row else None


def list_sessions():
    """Return all sessions, newest-first by last_active_ts."""
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY last_active_ts DESC").fetchall()
    finally:
        conn.close()
    return [_row_to_session(r) for r in rows]


def touch(sid, **fields):
    """Merge fields into the session, bump last_active_ts, and persist.

    An explicit `last_active_ts` in `fields` is honoured instead of being overwritten with
    now. Callers that want the bump simply do not pass it. This exists because the only way
    to place a session at a chosen point in the ordering used to be writing its file behind
    the store's back, and a store whose own tests must bypass it has an API gap, not a
    clever test.
    """
    if not _valid_sid(sid):
        raise ValueError("invalid session id")
    conn = _db()
    try:
        row = conn.execute("SELECT * FROM sessions WHERE sid = ?", (sid,)).fetchone()
        sess = _row_to_session(row) if row else {
            "sid": sid, "title": "", "conv_url": "", "created_ts": time.time(),
            "last_active_ts": time.time(), "status": "active", "turns": 0,
            "transcript": _transcript_ref(sid), "pending": [],
        }
        sess.update(fields)
        if "last_active_ts" not in fields:
            sess["last_active_ts"] = time.time()
        _write_session(conn, sess)
    finally:
        conn.close()
    return sess


def append_turn(sid, role, text):
    """Record one turn, in the table and in the compatibility transcript file."""
    if not _valid_sid(sid):
        raise ValueError("invalid session id")
    _ensure_dir()
    conn = _db()
    try:
        row = conn.execute("SELECT * FROM sessions WHERE sid = ?", (sid,)).fetchone()
        sess = _row_to_session(row) if row else None
        title = sess.get("title", "") if sess else ""
        turn_num = (sess.get("turns", 0) if sess else 0) + 1
        now = time.time()
        conn.execute(
            "INSERT OR REPLACE INTO turns (sid, turn, role, text, ts) VALUES (?, ?, ?, ?, ?)",
            (sid, turn_num, role, text, now))
        base = sess or {"sid": sid, "title": "", "conv_url": "", "created_ts": now,
                        "status": "active", "transcript": _transcript_ref(sid), "pending": []}
        base = dict(base, turns=turn_num, last_active_ts=now)
        _write_session(conn, base)
    finally:
        conn.close()

    # The cockpit reads these. Appended after the insert so the file can lag the table by a
    # crash but never lead it -- a transcript line with no row behind it is the confusing way
    # round.
    path = _transcript_path(sid)
    lines = []
    if not os.path.isfile(path):
        lines.append(json.dumps({"meta": True, "sid": sid, "title": title, "ts": now},
                                ensure_ascii=False))
    lines.append(json.dumps({"turn": turn_num, "role": role, "text": text, "ts": now},
                            ensure_ascii=False))
    try:
        with open(path, "a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line + "\n")
    except OSError:
        pass                    # the row is already committed; the export is best effort


def recent_turns(sid, limit=20):
    """The last `limit` turns, oldest-first. The reason this store exists.

    Re-supplying context after a recycle needs a BOUNDED read. Over a JSONL file that meant
    parsing the whole thing, which grows without limit, so the bridge recycled its
    conversation and simply lost what came before. Here it is an indexed lookup.
    """
    if not _valid_sid(sid):
        return []
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT turn, role, text, ts FROM turns WHERE sid = ? "
            "ORDER BY turn DESC LIMIT ?", (sid, int(limit))).fetchall()
    finally:
        conn.close()
    return [{"turn": r["turn"], "role": r["role"], "text": r["text"], "ts": r["ts"]}
            for r in reversed(rows)]


def all_turns(sid):
    """Every turn for a session, oldest-first."""
    if not _valid_sid(sid):
        return []
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT turn, role, text, ts FROM turns WHERE sid = ? ORDER BY turn",
            (sid,)).fetchall()
    finally:
        conn.close()
    return [{"turn": r["turn"], "role": r["role"], "text": r["text"], "ts": r["ts"]}
            for r in rows]


def search_turns(sid=None, needle="", limit=50):
    """Turns containing `needle`, newest-first, optionally within one session.

    The other half of what a file store could not do: finding something said weeks ago
    without reading every transcript on disk.
    """
    if not needle:
        return []
    conn = _db()
    try:
        if sid is not None:
            if not _valid_sid(sid):
                return []
            rows = conn.execute(
                "SELECT sid, turn, role, text, ts FROM turns "
                "WHERE sid = ? AND text LIKE ? ESCAPE '\\' ORDER BY ts DESC LIMIT ?",
                (sid, "%" + _like_escape(needle) + "%", int(limit))).fetchall()
        else:
            rows = conn.execute(
                "SELECT sid, turn, role, text, ts FROM turns "
                "WHERE text LIKE ? ESCAPE '\\' ORDER BY ts DESC LIMIT ?",
                ("%" + _like_escape(needle) + "%", int(limit))).fetchall()
    finally:
        conn.close()
    return [{"sid": r["sid"], "turn": r["turn"], "role": r["role"],
             "text": r["text"], "ts": r["ts"]} for r in rows]


def _like_escape(s):
    """A search for "100%" must not match everything."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def queue_input(sid, text):
    """Append text to the session's pending list."""
    if not _valid_sid(sid):
        raise ValueError("invalid session id")
    sess = load(sid)
    pending = list(sess.get("pending", [])) if sess else []
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
    conn = _db()
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE conv_url <> '' "
            "ORDER BY last_active_ts DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return _row_to_session(row) if row else None
