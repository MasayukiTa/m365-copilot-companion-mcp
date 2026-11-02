#!/usr/bin/env python3
"""Search the fleet conversation store: full text, by domain, and by refuter verdict.

This reads the structures scripts/migrate_sessions_search.py builds -- the fleet_turns_fts
trigram index, the verdict column, and the goal_summary rollup -- and never writes. It is the
query side of 'the chat is in a local database you can search': find a turn by the words in
it, list the goals in a domain, or pull every goal whose review landed on a given verdict.

WHY A TRIGRAM INDEX AND A LIKE FALLBACK, NOT ONE OR THE OTHER. The store is largely Japanese,
where a meaningful query is often two characters (解析, 製造, 検証). FTS5 trigram only indexes
runs of three or more characters, so a 1-2 character query matches nothing in the index -- not
an error, just silently empty, which is the kind of lie this whole task exists to stop. So a
query short enough that trigram cannot see it falls back to an indexed-by-nothing LIKE scan,
which is slower but correct; a query of three or more characters uses the index. The caller is
told which path answered in `engine`, so a slow short query is explained rather than mysterious.
"""
import argparse
import json
import os
import sqlite3
import sys

#: FTS5 trigram indexes runs of >= 3 characters; below that the index cannot answer.
_TRIGRAM_MIN = 3


#: Repo root = this file's parent's parent (tools/ sits directly under the repo), the same
#: anchor bridge/session_store.py uses (Path(__file__).resolve().parent.parent). Derived, not
#: hard-coded, so the store is found on any checkout and no operator's home path is baked in.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_db_path():
    # Mirror bridge/session_store._db_path() without importing the bridge (which pulls in the
    # whole runtime). Precedence: an explicit full DB path (MCP_SESSIONS_DB), then the store
    # directory the writers honour (MCP_SESSION_STORE_DIR), then <repo>/.fleet/sessions.
    full = os.environ.get("MCP_SESSIONS_DB")
    if full:
        return full
    base = os.environ.get("MCP_SESSION_STORE_DIR") or os.path.join(_REPO, ".fleet", "sessions")
    return os.path.join(base, "sessions.sqlite3")


def _connect_ro(path):
    """Read-only connection. immutable=1 so a query never blocks the fleet's writers and never
    takes the mmap path that crashes on a full disk."""
    uri = "file:%s?mode=ro&immutable=1" % path.replace("?", "")
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    return conn


def _like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _has_fts(conn):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fleet_turns_fts'"
    ).fetchone() is not None


def _goal_of(conn, row):
    """Coalesce the interned goal (goal_id -> fleet_goals) with any legacy inline goal."""
    if row["goal"]:
        return row["goal"]
    gid = row["goal_id"]
    if gid is None:
        return ""
    r = conn.execute("SELECT goal FROM fleet_goals WHERE goal_id = ?", (gid,)).fetchone()
    return r["goal"] if r else ""


def search_text(conn, needle, limit=25, domain=None):
    """Turns whose text contains `needle`, newest first. Returns (engine, rows).

    engine is 'fts' when the trigram index answered, 'like' when the query was too short for
    it and a scan answered instead.
    """
    needle = (needle or "").strip()
    if not needle:
        return "none", []
    use_fts = _has_fts(conn) and len(needle) >= _TRIGRAM_MIN
    params = []
    if use_fts:
        sql = ("SELECT t.id, t.key, t.role, t.text, t.ts, t.goal, t.goal_id, t.verdict "
               "FROM fleet_turns_fts f JOIN fleet_turns t ON t.id = f.rowid "
               "WHERE f.text MATCH ? ")
        params.append(_fts_query(needle))
        engine = "fts"
    else:
        sql = ("SELECT t.id, t.key, t.role, t.text, t.ts, t.goal, t.goal_id, t.verdict "
               "FROM fleet_turns t WHERE t.text LIKE ? ESCAPE '\\' ")
        params.append("%" + _like_escape(needle) + "%")
        engine = "like"
    if domain:
        sql += ("AND t.goal_id IN (SELECT goal_id FROM goal_summary WHERE domain = ?) ")
        params.append(domain)
    sql += "ORDER BY t.ts DESC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    out = [{"id": r["id"], "key": r["key"], "role": r["role"], "ts": r["ts"],
            "verdict": r["verdict"], "goal": _goal_of(conn, r),
            "text": r["text"]} for r in rows]
    return engine, out


def _fts_query(needle):
    """Quote the needle as a single FTS5 phrase so punctuation is not read as syntax."""
    return '"' + needle.replace('"', '""') + '"'


def list_by_domain(conn, domain, limit=50):
    rows = conn.execute(
        "SELECT goal, domain, turn_count, worker_count, first_ts, last_ts, has_refuter, "
        "final_verdict FROM v_goal_summary WHERE domain = ? ORDER BY last_ts DESC LIMIT ?",
        (domain, int(limit))).fetchall()
    return [dict(r) for r in rows]


def list_by_verdict(conn, verdict, limit=50):
    rows = conn.execute(
        "SELECT goal, domain, turn_count, worker_count, first_ts, last_ts, final_verdict "
        "FROM v_goal_summary WHERE final_verdict = ? ORDER BY last_ts DESC LIMIT ?",
        (verdict, int(limit))).fetchall()
    return [dict(r) for r in rows]


def domains(conn):
    rows = conn.execute(
        "SELECT domain, COUNT(*) AS goals, SUM(turn_count) AS turns "
        "FROM goal_summary GROUP BY domain ORDER BY goals DESC").fetchall()
    return [dict(r) for r in rows]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("text", help="full-text search of turns")
    t.add_argument("needle")
    t.add_argument("--domain")
    t.add_argument("--limit", type=int, default=25)
    d = sub.add_parser("domain", help="goals in a domain")
    d.add_argument("name")
    d.add_argument("--limit", type=int, default=50)
    v = sub.add_parser("verdict", help="goals whose review landed on a verdict")
    v.add_argument("name")
    v.add_argument("--limit", type=int, default=50)
    sub.add_parser("domains", help="domain histogram")
    args = ap.parse_args(argv)

    path = args.db or _default_db_path()
    conn = _connect_ro(path)
    try:
        if args.cmd == "text":
            engine, rows = search_text(conn, args.needle, args.limit, args.domain)
            print(json.dumps({"engine": engine, "count": len(rows), "rows": rows},
                             ensure_ascii=False, indent=2))
        elif args.cmd == "domain":
            print(json.dumps(list_by_domain(conn, args.name, args.limit),
                             ensure_ascii=False, indent=2))
        elif args.cmd == "verdict":
            print(json.dumps(list_by_verdict(conn, args.name, args.limit),
                             ensure_ascii=False, indent=2))
        elif args.cmd == "domains":
            print(json.dumps(domains(conn), ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
