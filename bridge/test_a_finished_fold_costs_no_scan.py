# -*- coding: utf-8 -*-
"""Once every goal is folded, recording a fleet turn must not scan the whole table.

_collapse_goals runs on every connection, and the fleet opens one per transcript line. Its
query is `WHERE goal <> ''`, which no index answered, so a store whose fold had long finished
still read all of fleet_turns to find nothing: 0.22 s per line warm on the 197 MB live store,
and 6.5 s for the first line of a cold fleet start -- the gap measured on 2026-09-24 between a
worker's `pending` and its transcript appearing.

The cost is counted in SQLite virtual-machine steps rather than seconds, so the test does not
depend on how fast the machine is: a scan is proportional to the rows, an index probe is not.
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


def _fill_folded(rows):
    conn = S._db(import_files=False)
    try:
        gid = S._intern_goal(conn, "an already-folded goal")
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT INTO fleet_turns (key, name, goal, goal_id, turn, role, text, extra, ts) "
            "VALUES (?, 'w0', '', ?, ?, 'user', ?, '{}', 0)",
            [("r%d_w0" % (i // 10), gid, i % 10, "t" * 200) for i in range(rows)])
        conn.execute("COMMIT")
    finally:
        conn.close()


def _steps(fn, conn):
    ticks = [0]

    def tick():
        ticks[0] += 1
        return 0

    conn.set_progress_handler(tick, 100)
    try:
        fn(conn)
    finally:
        conn.set_progress_handler(None, 100)
    return ticks[0]


def test_a_finished_fold_does_not_read_the_table(store):
    _fill_folded(20000)
    conn = S._connect()
    try:
        S._initialize(conn)
        steps = _steps(S._collapse_goals, conn)
    finally:
        conn.close()
    # A full scan of 20,000 rows is several thousand ticks of 100 steps; a probe of an empty
    # partial index is a handful.
    assert steps < 20, "%d x100 VM steps to find nothing to fold" % steps


def test_rows_that_still_need_folding_are_still_folded(store):
    """ORDER-DEPENDENT FAILURE, ROOT CAUSE (2026-09-24): this asserted `_collapse_goals(conn)
    == 30` from a SINGLE call. `_collapse_goals` is bounded by GOAL_COLLAPSE_SECONDS -- its own
    docstring: "a bounded slice at a time" -- and its normal caller (one connection per fleet
    turn, see `_db`/`_migrate`) never assumes one call finishes the backlog; the NEXT
    connection's call picks up whatever is left. On this machine, alone, folding 30 rows is
    fast enough that a single call always finishes -- but inside the full `bridge/` run
    (hundreds of tests before it, each creating and tearing down its own tmp sqlite file) disk
    and AV-scan contention accumulate, and a single 0.5s budget can legitimately expire mid-pass
    with rows still unfolded, at which point `done == 30` is false though nothing is broken.
    The exact-equality assertion was checking a promise the function never made. Drive the fold
    to completion the way production traffic eventually does (repeated calls, one per
    connection) and check the outcome that actually matters: everything that needed folding got
    folded, exactly once, and nothing is left over."""
    _fill_folded(500)
    conn = S._db(import_files=False)
    try:
        conn.executemany(
            "INSERT INTO fleet_turns (key, name, goal, turn, role, text, extra, ts) "
            "VALUES ('old_w0', 'w0', ?, ?, 'user', '', '{}', 0)",
            [("an inline goal from before interning", i) for i in range(30)])
        done = 0
        while True:
            n = S._collapse_goals(conn)
            if not n:
                break
            done += n
        left = conn.execute("SELECT count(*) FROM fleet_turns WHERE goal <> ''").fetchone()[0]
    finally:
        conn.close()
    assert done == 30 and left == 0


def test_a_new_turn_never_enters_the_unfolded_index(store):
    assert S.record_fleet_turn("r1_w0", {"turn": 1, "role": "user", "text": "hi", "ts": 1.0},
                               name="w0", goal="a goal")
    conn = S._connect()
    try:
        n = conn.execute("SELECT count(*) FROM fleet_turns INDEXED BY fleet_turns_unfolded_idx "
                         "WHERE goal <> ''").fetchone()[0]
        total = conn.execute("SELECT count(*) FROM fleet_turns").fetchone()[0]
    finally:
        conn.close()
    assert total == 1 and n == 0
