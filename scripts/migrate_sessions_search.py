#!/usr/bin/env python3
"""Add full-text search and goal-summary structures to a fleet sessions.sqlite3 store.

WHY A SEPARATE SCRIPT AND NOT THE HOT PATH. bridge/session_store.py builds the schema on
every connection in _initialize(), and the fleet opens one connection per transcript line it
records. The structures here are cheap to *declare* (CREATE IF NOT EXISTS) but expensive to
*fill*: the FTS index and the per-goal rollups must read every one of ~272k rows once. Doing
that on a connection the bridge is about to write a turn through is the exact mistake
_collapse_goals documents paying for (47s per connection). So declaration can live in
_initialize (empty, instant), but the one-time backfill is here, bounded and resumable, run
by an operator or a job -- never by a turn.

IDEMPOTENT. Re-running never duplicates: the FTS mirror is reconciled by rowid, the goal
rollup is an INSERT OR REPLACE keyed on goal_id, and verdict is derived from text every time.
A run that is killed half way leaves a consistent store and the next run resumes from the
high-water mark it persisted.

SAFE ON A FULL DISK. The live C: has been as low as 1.6 GB free and the store is ~2.4 GB, so
this never copies the database by default. It opens the real store read/write in place and
uses a rollback journal (not WAL -- the same 0xc0000006 mmap crash session_store.py avoids).
Pass --synthetic to build and migrate a small throwaway store for testing instead.
"""
import argparse
import json
import os
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_DOMAIN_RULES_PATH = os.path.join(_ROOT, "tools", "domain_rules.json")

#: verdict vocabulary actually present in the live store (measured 2026-11-02 over the
#: refuter_verdict rows): UPHELD / REFUTED / UNCLEAR / INCONCLUSIVE. The leading word of the
#: text is the verdict; anything else is left NULL rather than guessed.
_VERDICTS = ("UPHELD", "REFUTED", "UNCLEAR", "INCONCLUSIVE")

#: The progress / high-water-mark table, so a killed run resumes instead of restarting.
_STATE_TABLE = "search_migration_state"


def _load_domain_rules(path=_DOMAIN_RULES_PATH):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    rules = [(r["domain"], [k.lower() for k in r["keywords"]]) for r in data.get("rules", [])]
    return rules, str(data.get("default_domain", "other"))


def classify_domain(goal, rules, default_domain):
    """First rule whose any keyword is a substring of the (lowercased) goal wins."""
    g = (goal or "").lower()
    if not g:
        return default_domain
    for domain, keywords in rules:
        for kw in keywords:
            if kw and kw in g:
                return domain
    return default_domain


def verdict_of(text):
    """The leading verdict word of a refuter_verdict text, or None."""
    if not text:
        return None
    head = text.strip().split(":", 1)[0].strip().split()
    if head and head[0] in _VERDICTS:
        return head[0]
    return None


def use_rollback_journal(conn):
    """Match session_store: a rollback journal maps nothing, so a full disk is catchable."""
    conn.execute("PRAGMA journal_mode=DELETE")


def connect(path):
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    use_rollback_journal(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def fts5_available(conn):
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._fts_probe USING fts5(x, tokenize='trigram')")
        conn.execute("DROP TABLE temp._fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def declare_schema(conn):
    """Everything CREATE-IF-NOT-EXISTS and instant. Mirror of what _initialize would own.

    Declared here too so the script is self-contained against a store made by an older build,
    and so a test can call exactly this to assert the shape without a backfill.
    """
    # verdict column on fleet_turns (nullable; only refuter_verdict rows ever carry one).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fleet_turns)")}
    if "verdict" not in cols:
        conn.execute("ALTER TABLE fleet_turns ADD COLUMN verdict TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS fleet_turns_verdict_idx "
                 "ON fleet_turns(verdict) WHERE verdict IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS fleet_turns_goalid_role_idx "
                 "ON fleet_turns(goal_id, role)")

    # Per-goal rollup. Keyed on goal_id so it joins straight to fleet_goals / fleet_turns.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS goal_summary (
            goal_id        INTEGER PRIMARY KEY,
            domain         TEXT NOT NULL DEFAULT 'other',
            turn_count     INTEGER NOT NULL DEFAULT 0,
            worker_count   INTEGER NOT NULL DEFAULT 0,
            first_ts       REAL,
            last_ts        REAL,
            has_refuter    INTEGER NOT NULL DEFAULT 0,
            final_verdict  TEXT,
            FOREIGN KEY (goal_id) REFERENCES fleet_goals(goal_id)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS goal_summary_domain_idx ON goal_summary(domain)")
    conn.execute("CREATE INDEX IF NOT EXISTS goal_summary_verdict_idx ON goal_summary(final_verdict)")

    # FTS5 external-content mirror of fleet_turns.text, trigram so a mid-word CJK substring
    # (the store is largely Japanese) matches. content=fleet_turns means the FTS table holds
    # only the index, not a second copy of the text.
    if not _table_exists(conn, "fleet_turns_fts"):
        conn.execute(
            "CREATE VIRTUAL TABLE fleet_turns_fts USING fts5("
            "text, content='fleet_turns', content_rowid='id', tokenize='trigram')")

    # Progress table for resumable backfill.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS %s (k TEXT PRIMARY KEY, v TEXT NOT NULL DEFAULT '')"
        % _STATE_TABLE)


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (name,)).fetchone() is not None


def declare_view(conn):
    """A human-readable join; verdict and domain in one place next to the goal text."""
    conn.execute("DROP VIEW IF EXISTS v_goal_summary")
    conn.execute(
        """CREATE VIEW v_goal_summary AS
           SELECT s.goal_id, g.goal, s.domain, s.turn_count, s.worker_count,
                  s.first_ts, s.last_ts, s.has_refuter, s.final_verdict
           FROM goal_summary s JOIN fleet_goals g ON g.goal_id = s.goal_id""")


def _get_state(conn, k, default="0"):
    row = conn.execute("SELECT v FROM %s WHERE k = ?" % _STATE_TABLE, (k,)).fetchone()
    return row[0] if row else default


def _set_state(conn, k, v):
    conn.execute("INSERT INTO %s (k, v) VALUES (?, ?) "
                 "ON CONFLICT(k) DO UPDATE SET v = excluded.v" % _STATE_TABLE, (k, str(v)))


def backfill_verdict(conn, batch=5000, log=print):
    """Fill verdict for refuter_verdict rows that do not have one yet. Resumable by id."""
    hi = int(_get_state(conn, "verdict_hi", "0"))
    done = 0
    while True:
        rows = conn.execute(
            "SELECT id, text FROM fleet_turns WHERE role = 'refuter_verdict' AND id > ? "
            "ORDER BY id LIMIT ?", (hi, batch)).fetchall()
        if not rows:
            break
        conn.execute("BEGIN")
        try:
            # Index by position (id, text) so this works whether or not the caller set a
            # row_factory; connect() uses sqlite3.Row but a test may pass a plain connection.
            for r in rows:
                conn.execute("UPDATE fleet_turns SET verdict = ? WHERE id = ?",
                             (verdict_of(r[1]), r[0]))
                hi = r[0]
            _set_state(conn, "verdict_hi", hi)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        done += len(rows)
        log("  verdict: %d rows (id<=%d)" % (done, hi))
    return done


def reset_fts(conn):
    """Drop the FTS mirror and its high-water mark so the next backfill rebuilds from zero.

    For when the mirror is suspected inconsistent (e.g. an interrupted older run that used a
    delete-then-insert). Cheaper than guessing: clear the index, clear fts_hi, let
    backfill_fts walk every id again insert-only.
    """
    if _table_exists(conn, "fleet_turns_fts"):
        conn.execute("INSERT INTO fleet_turns_fts(fleet_turns_fts) VALUES ('delete-all')")
    _set_state(conn, "fts_hi", "0")
    # Close any implicit transaction the writes above opened, so a caller that connected with
    # the stdlib default (deferred) isolation can issue its own BEGIN next without hitting
    # 'cannot start a transaction within a transaction'. On an autocommit connection (the one
    # connect() hands out) this is a harmless no-op.
    if conn.in_transaction:
        conn.commit()


def backfill_fts(conn, batch=5000, log=print):
    """Reconcile the FTS mirror with fleet_turns by rowid. Resumable, idempotent.

    External-content FTS does not populate itself from existing rows, and a plain rebuild
    ('insert into fts(fts) values (rebuild)') reads the whole table in one unbounded pass.
    This walks ids in batches so a kill resumes, and skips ids already mirrored.
    """
    hi = int(_get_state(conn, "fts_hi", "0"))
    done = 0
    while True:
        rows = conn.execute(
            "SELECT id, text FROM fleet_turns WHERE id > ? ORDER BY id LIMIT ?",
            (hi, batch)).fetchall()
        if not rows:
            break
        conn.execute("BEGIN")
        try:
            for r in rows:
                # INSERT ONLY. We walk strictly ascending ids above the persisted high-water
                # mark (id > hi), so a row is mirrored exactly once across the whole run and
                # across resumes -- there is nothing to delete first. A 'delete' against an
                # external-content FTS asserts the row is already indexed with that text,
                # which is false on the first pass and raises 'database disk image is
                # malformed'. A from-scratch re-run resets fts_hi and rebuilds; reset_fts()
                # does exactly that.
                conn.execute(
                    "INSERT INTO fleet_turns_fts(rowid, text) VALUES (?, ?)",
                    (r[0], r[1]))
                hi = r[0]
            _set_state(conn, "fts_hi", hi)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        done += len(rows)
        log("  fts: %d rows (id<=%d)" % (done, hi))
    return done


def rebuild_goal_summary(conn, rules, default_domain, log=print):
    """Recompute the per-goal rollup from fleet_turns. Full recompute, but cheap and atomic.

    Not resumable by design: it is a single GROUP BY over an indexed join, and a partial
    rollup is worse than none. INSERT OR REPLACE keyed on goal_id makes a re-run converge.
    """
    # Aggregate facts per goal_id in SQL; classify domain and pick final verdict in Python.
    agg = conn.execute(
        """SELECT t.goal_id AS goal_id,
                  COUNT(*) AS turn_count,
                  COUNT(DISTINCT t.key) AS worker_count,
                  MIN(t.ts) AS first_ts,
                  MAX(t.ts) AS last_ts,
                  SUM(CASE WHEN t.role LIKE 'refuter%' THEN 1 ELSE 0 END) AS refuter_rows
           FROM fleet_turns t
           WHERE t.goal_id IS NOT NULL
           GROUP BY t.goal_id""").fetchall()
    # final verdict per goal = verdict of the newest refuter_verdict row for that goal.
    finals = {}
    for r in conn.execute(
            """SELECT goal_id, verdict FROM fleet_turns
               WHERE role = 'refuter_verdict' AND goal_id IS NOT NULL AND verdict IS NOT NULL
               AND id IN (SELECT MAX(id) FROM fleet_turns
                          WHERE role = 'refuter_verdict' AND goal_id IS NOT NULL
                          GROUP BY goal_id)"""):
        finals[r["goal_id"]] = r["verdict"]
    goals = {r["goal_id"]: r["goal"] for r in conn.execute("SELECT goal_id, goal FROM fleet_goals")}
    conn.execute("BEGIN")
    try:
        n = 0
        for r in agg:
            gid = r["goal_id"]
            domain = classify_domain(goals.get(gid, ""), rules, default_domain)
            conn.execute(
                """INSERT OR REPLACE INTO goal_summary
                   (goal_id, domain, turn_count, worker_count, first_ts, last_ts,
                    has_refuter, final_verdict)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (gid, domain, r["turn_count"], r["worker_count"], r["first_ts"], r["last_ts"],
                 1 if r["refuter_rows"] else 0, finals.get(gid)))
            n += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    log("  goal_summary: %d goals" % n)
    return len(agg)


def migrate(path, rules=None, default_domain=None, batch=5000, reset_fts_first=False, log=print):
    if rules is None:
        rules, default_domain = _load_domain_rules()
    conn = connect(path)
    try:
        if not fts5_available(conn):
            raise RuntimeError("this sqlite build has no FTS5; cannot create the search index")
        log("declaring schema ...")
        declare_schema(conn)
        log("backfilling verdict ...")
        backfill_verdict(conn, batch=batch, log=log)
        if reset_fts_first:
            # Operator asked to rebuild the FTS mirror from zero (suspected-inconsistent
            # index). Clear it and the high-water mark here so the backfill below walks every
            # id again insert-only rather than skipping ids it thinks are already mirrored.
            log("resetting fts mirror ...")
            reset_fts(conn)
        log("backfilling fts ...")
        backfill_fts(conn, batch=batch, log=log)
        log("rebuilding goal_summary ...")
        rebuild_goal_summary(conn, rules, default_domain, log=log)
        declare_view(conn)
        log("optimizing fts ...")
        conn.execute("INSERT INTO fleet_turns_fts(fleet_turns_fts) VALUES ('optimize')")
        list(conn.execute("PRAGMA incremental_vacuum"))
        log("done.")
    finally:
        conn.close()


def build_synthetic(path, n_goals=12, n_turns=400):
    """A tiny store with the real schema and a verdict/domain spread, for tests."""
    if os.path.exists(path):
        os.remove(path)
    conn = connect(path)
    conn.executescript(
        """
        CREATE TABLE fleet_goals (goal_id INTEGER PRIMARY KEY AUTOINCREMENT, goal TEXT NOT NULL UNIQUE);
        CREATE TABLE fleet_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
            goal TEXT NOT NULL DEFAULT '', turn INTEGER, role TEXT NOT NULL,
            text TEXT NOT NULL DEFAULT '', extra TEXT NOT NULL DEFAULT '{}', ts REAL NOT NULL,
            goal_id INTEGER);
        CREATE INDEX fleet_turns_key_idx ON fleet_turns(key, id);
        """)
    sample_goals = [
        "\u88fd\u9020\u30ed\u30c3\u30c8\u306e\u6b69\u7559\u307e\u308a\u3092\u89e3\u6790",  # manufacturing_analysis
        "\u30d4\u30fc\u30eb\u5024\u306e\u304b\u3059\u308c\u3092\u8abf\u67fb",            # manufacturing_analysis
        "slide \u3092 10 \u679a\u4f5c\u6210",                                           # slides
        "\u30e1\u30fc\u30eb\u306e\u8fd4\u4fe1\u3092\u4f5c\u6210",                        # mail
        "\u691c\u8a3c: claim \u3092 refuter \u3067\u78ba\u8a8d",                         # validity
        "PR #304 \u3092\u4fee\u6b63",                                                    # code
        "\u4f1a\u8b70\u306e\u4e88\u5b9a\u3092\u8abf\u6574",                              # calendar
        "random goal with no domain",                                                    # other
    ]
    # One transaction for all the seed inserts. connect() hands out an autocommit connection
    # (isolation_level=None) over a rollback journal, so without this each INSERT fsyncs its
    # own journal -- ~400 fsyncs is tens of seconds on this disk and was the whole cost of a
    # test's setup. Wrapping them makes build_synthetic sub-second.
    conn.execute("BEGIN")
    try:
        gids = []
        for g in sample_goals:
            conn.execute("INSERT OR IGNORE INTO fleet_goals (goal) VALUES (?)", (g,))
            gids.append(conn.execute(
                "SELECT goal_id FROM fleet_goals WHERE goal = ?", (g,)).fetchone()[0])
        verdicts = ["UPHELD", "REFUTED", "UNCLEAR", "INCONCLUSIVE"]
        ts = 1_700_000_000.0
        for i in range(n_turns):
            gid = gids[i % len(gids)]
            key = "w%d_a0" % (i % 5)
            conn.execute("INSERT INTO fleet_turns (key, role, text, ts, goal_id, turn) "
                         "VALUES (?, 'assistant', ?, ?, ?, ?)",
                         (key, "body %d \u672c\u6587" % i, ts + i, gid, i))
            if i % 7 == 0:
                v = verdicts[i % len(verdicts)]
                conn.execute("INSERT INTO fleet_turns (key, role, text, ts, goal_id, turn) "
                             "VALUES (?, 'refuter_verdict', ?, ?, ?, ?)",
                             (key, "%s: reason here" % v, ts + i + 0.5, gid, i))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.close()
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", help="path to sessions.sqlite3 to migrate in place")
    ap.add_argument("--synthetic", metavar="PATH",
                    help="build a throwaway synthetic store at PATH and migrate it")
    ap.add_argument("--batch", type=int, default=5000)
    ap.add_argument("--reset-fts", action="store_true",
                    help="clear and rebuild the FTS mirror from scratch (use when the index "
                         "is suspected inconsistent, e.g. after an interrupted older run)")
    args = ap.parse_args(argv)
    if not args.db and not args.synthetic:
        ap.error("pass --db PATH or --synthetic PATH")
    if args.synthetic:
        print("building synthetic store at", args.synthetic)
        build_synthetic(args.synthetic)
        migrate(args.synthetic, batch=args.batch, reset_fts_first=args.reset_fts)
    if args.db:
        t0 = time.time()
        migrate(args.db, batch=args.batch, reset_fts_first=args.reset_fts)
        print("migrated in %.1fs" % (time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
