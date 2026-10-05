"""Put the chats that happened after the bridge stopped recording them back into `sessions`.

THE GAP. The cockpit chat window kept being used after 2026-09-18, and `sessions` has no row for
any of it: the window's follow-ups into a past conversation do not go through the bridge's
/stream at all, they go to the fleet as an `add_goal` carrying the framing "[ユーザーからの追加指示]",
and the fleet records them in `fleet_turns` (and the transcript files), never in `sessions`.
The words are on disk; the table that was supposed to be the record of chats does not have them.

WHAT THIS DOES. Finds every fleet conversation (`fleet_turns.key`) that holds a user turn written
by that follow-up framing on or after --since, and copies its user/assistant turns into one
`sessions` row per conversation (source = 'fleet-chat') plus `turns`. Nothing is removed or
rewritten; fleet_turns is read only.

  python scripts/backfill_chat_sessions.py                      # dry run: counts only
  python scripts/backfill_chat_sessions.py --apply              # write, in ONE transaction

SAFE TO RUN TWICE. The session id is derived from the conversation key, and an id that already
exists in `sessions` is skipped, so a second run adds nothing.

REFUSES, and says why, when: free space on the store's drive is below --min-free-gb (1.5), or the
database is locked by a writer. It prints COUNTS ONLY -- never a title, a goal or a line of any
conversation. --store-dir points at a different store (a test's synthetic one); the default is the
live one, and the live one is run by the operator once the product is idle.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import shutil
import sqlite3
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

#: The text the chat window's follow-up framing puts at the start of the goal a human typed into a
#: past conversation (ui/ChatSend.DecideFleetSend). The fleet's own automatic goals do not carry it.
FOLLOW_UP_MARKER = "ユーザーからの追加指示"
DEFAULT_SINCE = "2026-09-18"
SOURCE = "fleet-chat"


def _since_epoch(text):
    d = datetime.datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=datetime.timezone(
        datetime.timedelta(hours=9)))
    return d.timestamp()


def _sid_for(key, first_ts, taken):
    """A valid session id derived from the conversation key, stable across runs."""
    stamp = time.strftime("%m%d%H%M%S", time.gmtime(first_ts))
    salt = 0
    while True:
        h = hashlib.sha1(("%s|%d" % (key, salt)).encode("utf-8")).hexdigest()[:4]
        sid = "s" + stamp + h
        if sid not in taken:
            return sid
        salt += 1


def _conversations(conn, since_ts):
    """[(key, [(role, text, ts)...], goal)] for the human follow-up conversations."""
    keys = [r[0] for r in conn.execute(
        "SELECT DISTINCT key FROM fleet_turns WHERE role = 'user' AND ts >= ? AND text LIKE ?",
        (since_ts, "%" + FOLLOW_UP_MARKER + "%"))]
    out = []
    for key in keys:
        rows = conn.execute(
            "SELECT role, text, ts, goal, goal_id FROM fleet_turns WHERE key = ? "
            "AND role IN ('user','assistant') ORDER BY id", (key,)).fetchall()
        turns = [(r[0], r[1], float(r[2])) for r in rows if (r[1] or "").strip()]
        goal = ""
        for r in rows:
            goal = r[3] or ""
            if not goal and r[4]:
                g = conn.execute("SELECT goal FROM fleet_goals WHERE goal_id = ?", (r[4],)).fetchone()
                goal = g[0] if g else ""
            if goal:
                break
        if turns:
            out.append((key, turns, goal))
    return out


def _plan(conn, since_ts):
    existing = {r[0] for r in conn.execute("SELECT sid FROM sessions")}
    planned, skipped, taken = [], 0, set(existing)
    for key, turns, goal in _conversations(conn, since_ts):
        sid = _sid_for(key, turns[0][2], set())      # stable: independent of what else exists
        if sid in existing:
            skipped += 1
            continue
        if sid in taken:                              # two keys, one id, in THIS run: rare
            sid = _sid_for(key, turns[0][2], taken)
        taken.add(sid)
        planned.append((sid, key, turns, goal))
    return planned, skipped


def _refuse_reason(store_dir, min_free_gb):
    probe = store_dir if os.path.isdir(store_dir) else os.path.dirname(store_dir) or "."
    free = shutil.disk_usage(probe).free / float(1 << 30)
    if free < min_free_gb:
        return "only %.2f GB free on the store's drive (need %.2f)" % (free, min_free_gb)
    return ""


def _write(conn, planned):
    """ONE transaction. Raises on any failure, which rolls the whole batch back."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        for sid, _key, turns, goal in planned:
            first, last = turns[0][2], turns[-1][2]
            conn.execute(
                "INSERT INTO sessions (sid, title, conv_url, created_ts, last_active_ts, status, "
                "turns, transcript, pending_json, mode, goal, source) "
                "VALUES (?, ?, '', ?, ?, 'done', ?, '', '[]', '', ?, ?)",
                (sid, (goal or "")[:80], first, last, len(turns), (goal or "")[:2000], SOURCE))
            for n, (role, text, ts) in enumerate(turns, 1):
                conn.execute("INSERT INTO turns (sid, turn, role, text, ts) VALUES (?, ?, ?, ?, ?)",
                             (sid, n, role, text, ts))
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    ap.add_argument("--since", default=DEFAULT_SINCE, help="YYYY-MM-DD (JST), default %(default)s")
    ap.add_argument("--min-free-gb", type=float, default=1.5)
    ap.add_argument("--store-dir", default="", help="a different store directory (tests)")
    args = ap.parse_args(argv)
    if args.store_dir:
        os.environ["MCP_SESSION_STORE_DIR"] = args.store_dir
    from bridge import session_store as S
    db_path = S._db_path()
    if not os.path.isfile(db_path):
        print("no session store at the configured location; nothing to do")
        return 1
    since_ts = _since_epoch(args.since)

    ro = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True, timeout=30)
    try:
        planned, skipped = _plan(ro, since_ts)
    finally:
        ro.close()
    n_turns = sum(len(p[2]) for p in planned)
    print("conversations to add: %d (turns: %d); already present: %d; since %s"
          % (len(planned), n_turns, skipped, args.since))
    if not args.apply:
        print("dry run: nothing written (pass --apply to write)")
        return 0
    why = _refuse_reason(os.path.dirname(db_path), args.min_free_gb)
    if why:
        print("refused: %s" % why)
        return 2
    if not planned:
        print("nothing to write")
        return 0
    try:
        conn = S._db(import_files=False)
    except S.StoreUnavailable as exc:
        print("refused: the session store cannot be opened for writing (%s); nothing was written"
              % type(exc.cause).__name__)
        return 2
    try:
        conn.execute("PRAGMA busy_timeout=2000")
        try:
            _write(conn, planned)
        except sqlite3.OperationalError as exc:
            print("refused: the database is busy or unwritable (%s); nothing was written"
                  % type(exc).__name__)
            return 2
    finally:
        conn.close()
    print("written: %d conversations, %d turns" % (len(planned), n_turns))
    return 0


if __name__ == "__main__":
    sys.exit(main())
