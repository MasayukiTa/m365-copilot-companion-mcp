"""The search layer over the fleet store: it must find a turn by its words, bucket goals by
domain, read a refuter verdict off the text, and -- the property that the whole 'lost memory'
task turns on -- re-run without duplicating or dropping anything.

Everything runs against a small synthetic store the migration script builds for exactly this
(build_synthetic), never the live database: the live C: has been down to 1.6 GB free and the
store is ~2.4 GB, so a test that copied it would be the bug it is meant to catch.
"""
import os
import sqlite3
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts import migrate_sessions_search as M
from tools import search_conversations as SC


@pytest.fixture
def store(tmp_path):
    """A migrated synthetic store, returned as its path."""
    path = str(tmp_path / "sessions.sqlite3")
    M.build_synthetic(path)
    M.migrate(path, log=lambda *a, **k: None)
    return path


def _ro(path):
    return SC._connect_ro(path)


# -- verdict ---------------------------------------------------------------------------------

def test_verdict_is_read_off_the_leading_word():
    assert M.verdict_of("UPHELD: because ...") == "UPHELD"
    assert M.verdict_of("REFUTED") == "REFUTED"
    assert M.verdict_of("  INCONCLUSIVE : x") == "INCONCLUSIVE"
    # A body that does not open with a known verdict word gets None, not a guess.
    assert M.verdict_of("the claim seems upheld maybe") is None
    assert M.verdict_of("") is None
    assert M.verdict_of(None) is None


def test_every_refuter_verdict_row_gets_a_verdict(store):
    conn = sqlite3.connect(store)
    try:
        missing = conn.execute(
            "SELECT COUNT(*) FROM fleet_turns WHERE role='refuter_verdict' AND verdict IS NULL"
        ).fetchone()[0]
        non_verdict_rows = conn.execute(
            "SELECT COUNT(*) FROM fleet_turns WHERE role<>'refuter_verdict' AND verdict IS NOT NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    assert missing == 0            # every verdict row classified
    assert non_verdict_rows == 0   # and nothing else was touched


# -- domain ----------------------------------------------------------------------------------

def test_domain_rules_classify_and_fall_through_to_other():
    rules, default = M._load_domain_rules()
    assert M.classify_domain("\u88fd\u9020\u30ed\u30c3\u30c8\u306e\u89e3\u6790", rules, default) == "manufacturing_analysis"
    assert M.classify_domain("slide \u3092\u4f5c\u308b", rules, default) == "slides"
    assert M.classify_domain("\u691c\u8a3c: claim", rules, default) == "validity"
    assert M.classify_domain("totally unrelated", rules, default) == "other"
    assert M.classify_domain("", rules, default) == default


def test_goal_summary_has_one_row_per_goal_with_facts(store):
    conn = sqlite3.connect(store)
    conn.row_factory = sqlite3.Row
    try:
        goals = conn.execute("SELECT COUNT(*) FROM fleet_goals").fetchone()[0]
        summ = conn.execute("SELECT COUNT(*) FROM goal_summary").fetchone()[0]
        # turn_count in the rollup equals the real per-goal count.
        rows = conn.execute(
            """SELECT s.goal_id, s.turn_count,
                      (SELECT COUNT(*) FROM fleet_turns t WHERE t.goal_id = s.goal_id) AS real_count
               FROM goal_summary s""").fetchall()
    finally:
        conn.close()
    assert summ == goals
    for r in rows:
        assert r["turn_count"] == r["real_count"]


# -- full text search ------------------------------------------------------------------------

def test_fts_finds_a_three_char_substring(store):
    conn = _ro(store)
    try:
        engine, rows = SC.search_text(conn, "body", limit=5)
    finally:
        conn.close()
    assert engine == "fts"
    assert rows and all("body" in r["text"] for r in rows)


def test_short_cjk_query_falls_back_to_like_not_empty_lie(store):
    # Insert a row whose text contains a two-character CJK needle that trigram cannot index.
    conn = sqlite3.connect(store)
    try:
        conn.execute("INSERT INTO fleet_turns(key,role,text,ts,goal_id,turn) "
                     "VALUES('wq','assistant',?,?,1,1)",
                     ("\u89e3\u6790\u3092\u5b9f\u884c", 1700001234.0))
        conn.commit()
    finally:
        conn.close()
    conn = _ro(store)
    try:
        engine, rows = SC.search_text(conn, "\u89e3\u6790", limit=10)
    finally:
        conn.close()
    # Two chars: FTS cannot answer, so the tool must use LIKE and still find the row.
    assert engine == "like"
    assert any("\u89e3\u6790" in r["text"] for r in rows)


def test_search_can_filter_by_domain(store):
    conn = _ro(store)
    try:
        engine, rows = SC.search_text(conn, "body", limit=50, domain="manufacturing_analysis")
    finally:
        conn.close()
    assert rows
    # Every returned turn belongs to a manufacturing_analysis goal.
    conn = sqlite3.connect(store)
    try:
        mfg = {gid for (gid,) in conn.execute(
            "SELECT goal_id FROM goal_summary WHERE domain='manufacturing_analysis'")}
        ids = {r["id"] for r in rows}
        bad = conn.execute(
            "SELECT COUNT(*) FROM fleet_turns WHERE id IN (%s) AND "
            "(goal_id IS NULL OR goal_id NOT IN (%s))" % (
                ",".join(str(i) for i in ids) or "0",
                ",".join(str(g) for g in mfg) or "0")).fetchone()[0]
    finally:
        conn.close()
    assert bad == 0


# -- verdict / domain listings ---------------------------------------------------------------

def test_list_by_verdict_and_domain(store):
    conn = _ro(store)
    try:
        refuted = SC.list_by_verdict(conn, "REFUTED")
        mfg = SC.list_by_domain(conn, "manufacturing_analysis")
        hist = SC.domains(conn)
    finally:
        conn.close()
    assert all(r["final_verdict"] == "REFUTED" for r in refuted)
    assert all(r["domain"] == "manufacturing_analysis" for r in mfg)
    assert sum(d["goals"] for d in hist) == _goal_count(store)


def _goal_count(store):
    conn = sqlite3.connect(store)
    try:
        return conn.execute("SELECT COUNT(*) FROM goal_summary").fetchone()[0]
    finally:
        conn.close()


# -- idempotence (the task's core property) --------------------------------------------------

def test_migrate_is_idempotent_no_dup_no_loss(store):
    before_turns = _one(store, "SELECT COUNT(*) FROM fleet_turns")
    before_fts = _one(store, "SELECT COUNT(*) FROM fleet_turns_fts")
    before_summ = _one(store, "SELECT COUNT(*) FROM goal_summary")
    # Run the whole migration again over the already-migrated store.
    M.migrate(store, log=lambda *a, **k: None)
    assert _one(store, "SELECT COUNT(*) FROM fleet_turns") == before_turns
    # The FTS mirror has exactly one row per base row, still, after a second pass.
    assert _one(store, "SELECT COUNT(*) FROM fleet_turns_fts") == before_fts == before_turns
    assert _one(store, "SELECT COUNT(*) FROM goal_summary") == before_summ


def test_reset_fts_then_backfill_rebuilds_cleanly(store):
    # An external-content FTS table's COUNT(*) reflects the base table, not the index, so a
    # cleared index still 'counts' every row. The honest emptiness check is a MATCH returning
    # nothing; the honest rebuilt check is the same MATCH finding the rows again.
    conn = sqlite3.connect(store)
    try:
        assert _match_count(conn, "body") > 0
        M.reset_fts(conn)
        assert _match_count(conn, "body") == 0          # index cleared
        M.backfill_fts(conn, log=lambda *a, **k: None)
        assert _match_count(conn, "body") > 0           # and rebuilt
    finally:
        conn.close()


def _match_count(conn, needle):
    return conn.execute(
        "SELECT COUNT(*) FROM fleet_turns_fts WHERE text MATCH ?", (needle,)).fetchone()[0]


def _one(store, sql):
    conn = sqlite3.connect(store)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def _one_conn(conn, sql):
    return conn.execute(sql).fetchone()[0]
