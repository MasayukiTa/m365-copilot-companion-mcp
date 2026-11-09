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
import datetime
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
    # whole runtime). The bridge resolves the store through exactly one override --
    # MCP_SESSION_STORE_DIR, the store directory the writers honour -- then <repo>/.fleet/
    # sessions. We follow that precedence to the letter: no second, full-path override of our
    # own (an MCP_SESSIONS_DB knob the writers never read would redirect the reader to a store
    # the writers never touch, and test isolation already covers MCP_SESSION_STORE_DIR). A
    # caller that wants a specific file passes --db.
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


def _parse_when(s):
    """Accept an epoch second (int/float) or an ISO-8601 instant and return a float epoch.

    A date alone ('2026-11-02') is read as that day's 00:00:00 UTC. Returns None for None/''.
    Raised as ValueError on anything else, so a typo is reported rather than silently ignored.
    """
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    iso = s.replace("Z", "+00:00")
    dt = datetime.datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def search(conn, q=None, domain=None, since=None, until=None, verdict=None,
           goal_id=None, limit=25):
    """Unified turn search. Any subset of filters may be combined; all are ANDed.

    q        -- substring of the turn text (FTS when >= 3 chars, LIKE fallback below that)
    domain   -- restrict to goals classified into this domain
    since/until -- inclusive ts (epoch-second) bounds, from _parse_when
    verdict  -- restrict to turns carrying this refuter verdict
    goal_id  -- restrict to a single goal
    Returns (engine, rows). engine is 'fts'/'like' when q was given, else 'filter'.
    """
    needle = (q or "").strip()
    where = []
    params = []
    if needle:
        use_fts = _has_fts(conn) and len(needle) >= _TRIGRAM_MIN
        if use_fts:
            base = ("SELECT t.id, t.key, t.role, t.text, t.ts, t.goal, t.goal_id, t.verdict "
                    "FROM fleet_turns_fts f JOIN fleet_turns t ON t.id = f.rowid "
                    "WHERE f.text MATCH ? ")
            params.append(_fts_query(needle))
            engine = "fts"
        else:
            base = ("SELECT t.id, t.key, t.role, t.text, t.ts, t.goal, t.goal_id, t.verdict "
                    "FROM fleet_turns t WHERE t.text LIKE ? ESCAPE '\\' ")
            params.append("%" + _like_escape(needle) + "%")
            engine = "like"
    else:
        base = ("SELECT t.id, t.key, t.role, t.text, t.ts, t.goal, t.goal_id, t.verdict "
                "FROM fleet_turns t WHERE 1=1 ")
        engine = "filter"
    if domain:
        where.append("t.goal_id IN (SELECT goal_id FROM goal_summary WHERE domain = ?)")
        params.append(domain)
    if since is not None:
        where.append("t.ts >= ?")
        params.append(float(since))
    if until is not None:
        where.append("t.ts <= ?")
        params.append(float(until))
    if verdict:
        where.append("t.verdict = ?")
        params.append(verdict)
    if goal_id is not None:
        where.append("t.goal_id = ?")
        params.append(int(goal_id))
    sql = base + "".join("AND " + c + " " for c in where) + "ORDER BY t.ts DESC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    out = [{"id": r["id"], "key": r["key"], "role": r["role"], "ts": r["ts"],
            "goal_id": r["goal_id"], "verdict": r["verdict"], "goal": _goal_of(conn, r),
            "text": r["text"]} for r in rows]
    return engine, out


def dump_rows(conn, q=None, domain=None, since=None, until=None, verdict=None,
              goal_id=None, limit=1000000):
    """Like search() but every matching turn is emitted whole and VERBATIM -- id, key, role,
    ts, goal_id, verdict and the full original `text` with no truncation or rewriting -- so a
    dump can be diffed against the source. Returns a list of dicts (newest first).
    """
    _, rows = search(conn, q=q, domain=domain, since=since, until=until, verdict=verdict,
                     goal_id=goal_id, limit=limit)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="store path (default: derived from repo root)")
    ap.add_argument("--q", default=None, help="substring to search in turn text")
    ap.add_argument("--domain", default=None, help="restrict to goals in this domain")
    ap.add_argument("--since", default=None, help="ts lower bound: epoch second or ISO-8601")
    ap.add_argument("--until", default=None, help="ts upper bound: epoch second or ISO-8601")
    ap.add_argument("--verdict", default=None, help="restrict to turns with this verdict")
    ap.add_argument("--goal-id", dest="goal_id", type=int, default=None,
                    help="restrict to a single goal_id")
    ap.add_argument("--limit", type=int, default=25, help="max rows (default 25)")
    ap.add_argument("--mode", default="search",
                    choices=("search", "text", "domain", "verdict", "domains"),
                    help="search: unified filter (default); text: FTS/LIKE on --q only; "
                         "domain: goals in --domain; verdict: goals with --verdict; "
                         "domains: the domain rollup")
    ap.add_argument("--dump", action="store_true",
                    help="emit every matching turn VERBATIM as JSONL (one row per line)")
    ap.add_argument("--out", default=None,
                    help="write output to this file instead of stdout")
    args = ap.parse_args(argv)

    since = _parse_when(args.since)
    until = _parse_when(args.until)
    path = args.db or _default_db_path()
    conn = _connect_ro(path)
    try:
        if args.dump:
            limit = args.limit if args.limit and args.limit > 0 else 1000000
            rows = dump_rows(conn, q=args.q, domain=args.domain, since=since, until=until,
                             verdict=args.verdict, goal_id=args.goal_id, limit=limit)
            text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        elif args.mode == "text":
            engine, rows = search_text(conn, args.q, limit=args.limit, domain=args.domain)
            text = json.dumps({"engine": engine, "count": len(rows), "rows": rows},
                              ensure_ascii=False, indent=2) + "\n"
        elif args.mode == "domain":
            rows = list_by_domain(conn, args.domain, limit=args.limit)
            text = json.dumps({"domain": args.domain, "count": len(rows), "rows": rows},
                              ensure_ascii=False, indent=2) + "\n"
        elif args.mode == "verdict":
            rows = list_by_verdict(conn, args.verdict, limit=args.limit)
            text = json.dumps({"verdict": args.verdict, "count": len(rows), "rows": rows},
                              ensure_ascii=False, indent=2) + "\n"
        elif args.mode == "domains":
            rows = domains(conn)
            text = json.dumps({"count": len(rows), "rows": rows},
                              ensure_ascii=False, indent=2) + "\n"
        else:
            engine, rows = search(conn, q=args.q, domain=args.domain, since=since, until=until,
                                  verdict=args.verdict, goal_id=args.goal_id, limit=args.limit)
            text = json.dumps({"engine": engine, "count": len(rows), "rows": rows},
                              ensure_ascii=False, indent=2) + "\n"
    finally:
        conn.close()
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        sys.stderr.write("wrote %d row(s) to %s\n" % (len(rows), args.out))
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
