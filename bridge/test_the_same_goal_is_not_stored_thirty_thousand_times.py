# -*- coding: utf-8 -*-
"""A fleet turn row carried the whole goal text, and a goal is run for many turns by many workers.

MEASURED ON THE LIVE STORE, 2026-09-14, before this existed:

    fleet_turns rows        30,903      over 4,242 keys
    goal bytes, total       74.08 MB
    goal bytes, distinct     5.12 MB    1,950 distinct goal texts
    all turn text           64.15 MB
    database                  265 MB

68.9 MB of pure duplication -- 26% of the database, and more than every turn's text put
together. Nothing ever removed it: `prune()` has never looked at fleet_turns (it deletes whole
SESSIONS, a different table), and `compact()`, the VACUUM that hands pages back, has no caller
anywhere in the repository.

INTERNED ON THE TEXT, NOT ON THE RUN KEY. The obvious normalisation is one goal per `key` --
and it is WRONG here: 60 keys in that store carry more than one distinct goal, so those rows
would have been given another goal silently. A duplicate-free store with the wrong goal on a row
is worse than a duplicated one. The key is the text.

AUTOMATIC, because the alternative is somebody remembering. It folds a bounded slice on each
connection rather than rewriting 30,903 rows inside one write transaction on the path a turn is
recorded through -- and stopping early costs nothing, because the reader coalesces the interned
goal with the old inline one and cannot tell which shape a row has.

MEASURED ON A COPY OF THE LIVE STORE, three times, because the first two bounds did not bound
what hurts. The fingerprint of (key, name, turn, role, ts, goal) over all 30,903 rows in id
order is identical before and after in every run -- b83189e783eebc95 -- so "lossless" is a
measurement here and not a design intention:

    bound                                  worst single open   opens   final size
    4,000 rows, no clock                        47 s              8     197.0 MB
    0.5 s checked between batches                2.35 s         155     197.0 MB
    0.5 s checked per row, 200-row reads         0.72 s         619     197.0 MB

265.8 MB -> 197.0 MB, 68.8 MB handed back to the filesystem. The last row is the one shipped:
a turn-recording path can afford 0.7 s and cannot afford 47.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import pytest  # noqa: E402

from bridge import session_store as S  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "_base_dir", lambda: str(tmp_path))
    return tmp_path


def _record(key, goal, n, name="w1"):
    """NOTE THE ARGUMENT ORDER: record_fleet_turn(key, obj, name=, goal=). The first draft of
    this helper passed them positionally in the order they appear in the ROW, which handed the
    worker name in as `obj` -- and `record_fleet_turn` never raises, so all five tests reported
    `assert False` about the code rather than about the call."""
    for i in range(n):
        assert S.record_fleet_turn(key,
                                   {"turn": i, "role": "assistant", "text": "t%d" % i,
                                    "ts": 1000.0 + i},
                                   name=name, goal=goal)


def _goal_bytes(conn):
    return conn.execute("SELECT COALESCE(SUM(LENGTH(goal)), 0) FROM fleet_turns").fetchone()[0]


# ── it is lossless ────────────────────────────────────────────────────────────────────────

def test_the_goal_comes_back_on_every_row(store):
    """THE ONLY THING THAT MATTERS FIRST. A compression that loses a goal is data loss with a
    size report attached."""
    _record("run1_w0", "fix the flaky mail test", 5)
    rows = S.fleet_turns("run1_w0")
    assert len(rows) == 5
    assert {r["goal"] for r in rows} == {"fix the flaky mail test"}


def test_two_goals_under_one_key_stay_apart(store):
    """THE REASON THIS IS NOT KEYED ON `key`. Sixty keys in the live store carry more than one
    distinct goal; a per-key goals table would have handed those rows another worker's goal and
    every count would still have looked right."""
    _record("shared_key", "goal A", 3)
    _record("shared_key", "goal B", 2)
    goals = sorted(r["goal"] for r in S.fleet_turns("shared_key"))
    assert goals == ["goal A", "goal A", "goal A", "goal B", "goal B"]


def test_a_row_written_before_the_fold_still_reads(store):
    """MIXED SHAPES ARE THE NORMAL STATE, not a migration corner: the fold runs a slice at a
    time, so at any moment some rows carry an interned id and some their own text. Both must
    read the same, or every reader gets a different answer depending on how far it has got."""
    _record("k", "an interned goal", 1)
    conn = S._db(import_files=False)
    try:
        conn.execute("INSERT INTO fleet_turns (key, name, goal, turn, role, text, extra, ts) "
                     "VALUES ('k', 'w1', 'an inline goal', 9, 'assistant', 'old', '{}', 2000.0)")
    finally:
        conn.close()
    assert {r["goal"] for r in S.fleet_turns("k")} == {"an interned goal", "an inline goal"}


def test_an_empty_goal_does_not_become_a_row(store):
    """A meta line has no goal, and interning "" would put one blank row in the goals table and
    point thousands of rows at it -- tidy-looking and meaningless."""
    assert S.record_fleet_turn("k", {"role": "meta", "text": "", "ts": 1.0},
                               name="w1", goal="")
    assert S.fleet_turns("k")[0]["goal"] == ""
    conn = S._db(import_files=False)
    try:
        assert conn.execute("SELECT COUNT(*) FROM fleet_goals").fetchone()[0] == 0
    finally:
        conn.close()


# ── it actually compresses ────────────────────────────────────────────────────────────────

def test_a_repeated_goal_is_stored_once(store):
    """The claim, measured rather than asserted."""
    goal = "G" * 500
    _record("k", goal, 40)
    conn = S._db(import_files=False)
    try:
        assert _goal_bytes(conn) == 0, "the goal text is still on the turn rows"
        assert conn.execute("SELECT COUNT(*) FROM fleet_goals").fetchone()[0] == 1
        assert conn.execute(
            "SELECT SUM(LENGTH(goal)) FROM fleet_goals").fetchone()[0] == len(goal)
    finally:
        conn.close()


def test_rows_that_predate_the_fold_are_folded_automatically(store):
    """THE HALF THAT MAKES IT A FIX RATHER THAN A POLICY FOR THE FUTURE. Interning only new
    writes would leave the 68.9 MB exactly where it is and report success.

    No one runs anything here: the rows are folded by opening the store."""
    conn = S._db(import_files=False)
    try:
        for i in range(30):
            conn.execute(
                "INSERT INTO fleet_turns (key, name, goal, turn, role, text, extra, ts) "
                "VALUES ('old', 'w1', ?, ?, 'assistant', 'x', '{}', 1.0)", ("Z" * 400, i))
        before = _goal_bytes(conn)
    finally:
        conn.close()
    assert before == 30 * 400

    S._db(import_files=False).close()          # one ordinary open -- that is the whole driver

    conn = S._db(import_files=False)
    try:
        assert _goal_bytes(conn) == 0, "opening the store did not fold the old rows"
    finally:
        conn.close()
    assert {r["goal"] for r in S.fleet_turns("old")} == {"Z" * 400}, "the fold lost the goal"


def test_a_bounded_fold_still_converges(store, monkeypatch):
    """A bound that never finishes is a leak with a nice comment. Repeated ordinary opens must
    reach zero, and the goal must survive all of them.

    THE ROW CAP IS NOT THE BOUND, and this test used to assume it was: it asserted that one
    connection with GOAL_COLLAPSE_BATCH=5 would leave some of 12 rows unfolded. That stopped
    being true when the budget started looping over batches -- the cap is now the READ size, and
    0.5 s is plenty for 12 tiny rows. The bound that matters is the clock, and
    test_the_clock_stops_a_pass_even_when_the_row_cap_does_not owns it. What is left to say here
    is that the fold terminates."""
    monkeypatch.setattr(S, "GOAL_COLLAPSE_BATCH", 5)
    monkeypatch.setattr(S, "GOAL_COLLAPSE_SECONDS", 0.0)   # one row per pass: the slowest case
    conn = S._db(import_files=False)
    try:
        for i in range(12):
            conn.execute(
                "INSERT INTO fleet_turns (key, name, goal, turn, role, text, extra, ts) "
                "VALUES ('b', 'w1', ?, ?, 'assistant', 'x', '{}', 1.0)", ("Q" * 10, i))
    finally:
        conn.close()

    for _ in range(20):
        S._db(import_files=False).close()
        conn = S._db(import_files=False)
        try:
            left = conn.execute(
                "SELECT COUNT(*) FROM fleet_turns WHERE goal <> ''").fetchone()[0]
        finally:
            conn.close()
        if left == 0:
            break
    assert left == 0, "the fold did not converge: %d rows left" % left
    assert {r["goal"] for r in S.fleet_turns("b")} == {"Q" * 10}, "converging lost the goal"


def test_the_clock_stops_a_pass_even_when_the_row_cap_does_not(store, monkeypatch):
    """THE BOUND THAT WAS MISSING. With only a row cap, one pass over rows carrying 2.4 KB of
    goal each took 47 seconds on the live store -- on the path a turn is recorded through.

    A zero-second budget must stop after ONE row rather than after `GOAL_COLLAPSE_BATCH` of
    them: the check is at the end of the loop body, so a pass always makes progress and can
    never spin without folding anything."""
    conn = S._db(import_files=False)
    try:
        for i in range(20):
            conn.execute(
                "INSERT INTO fleet_turns (key, name, goal, turn, role, text, extra, ts) "
                "VALUES ('t', 'w1', ?, ?, 'assistant', 'x', '{}', 1.0)", ("T" * 50, i))
    finally:
        conn.close()

    monkeypatch.setattr(S, "GOAL_COLLAPSE_SECONDS", 0.0)
    monkeypatch.setattr(S, "GOAL_COLLAPSE_BATCH", 20)
    S._db(import_files=False).close()

    conn = S._db(import_files=False)          # this open folds one more
    try:
        left = conn.execute(
            "SELECT COUNT(*) FROM fleet_turns WHERE goal <> ''").fetchone()[0]
    finally:
        conn.close()
    assert left >= 17, "a zero-second budget folded %d rows in two passes" % (20 - left)
    assert left < 20, "a zero-second budget made no progress at all"


def test_a_fold_that_fails_does_not_stop_the_store_being_written(store, monkeypatch):
    """This sits on the connection path. A store that cannot be folded must still be a store."""
    def _boom(_conn, _goal):
        raise RuntimeError("no")

    monkeypatch.setattr(S, "_intern_goal", _boom)
    conn = S._db(import_files=False)
    try:
        conn.execute("INSERT INTO fleet_turns (key, name, goal, turn, role, text, extra, ts) "
                     "VALUES ('c', 'w1', 'g', 1, 'assistant', 'x', '{}', 1.0)")
    finally:
        conn.close()
    S._db(import_files=False).close()
    assert S.fleet_turns("c")[0]["goal"] == "g"
