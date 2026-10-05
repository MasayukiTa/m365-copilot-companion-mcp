# -*- coding: utf-8 -*-
"""scripts/backfill_chat_sessions.py, run ONLY against a small synthetic store.

It is never run against the live database from here. The operator runs it, once, after the change
is merged and the product is idle; what this file pins is the behaviour that makes that safe:
dry run by default, one transaction, idempotent, refuses on low disk or a locked store, counts
only on stdout.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET_GOAL = "SECRET-GOAL-TEXT-DO-NOT-PRINT"
SECRET_LINE = "SECRET-CHAT-LINE-DO-NOT-PRINT"


@pytest.fixture()
def synth(tmp_path, monkeypatch):
    store_dir = str(tmp_path / "sessions")
    monkeypatch.setenv("MCP_SESSION_STORE_DIR", store_dir)
    from bridge import session_store as S
    conn = S._db(import_files=False)
    now = time.time()
    marker = "【ユーザーからの追加指示】"

    def add(key, role, text, ts, goal=SECRET_GOAL):
        conn.execute("INSERT INTO fleet_turns (key, name, goal, turn, role, text, extra, ts) "
                     "VALUES (?, 'w0', ?, 1, ?, ?, '{}', ?)", (key, goal, role, text, ts))
    # a human follow-up conversation, after the cut-off
    add("r1_a0_w0", "user", "first goal", now - 100)
    add("r1_a0_w0", "assistant", "first answer", now - 90)
    add("r1_a0_w0", "user", marker + SECRET_LINE, now - 80)
    add("r1_a0_w0", "assistant", "second answer", now - 70)
    add("r1_a0_w0", "meta", "not a chat turn", now - 60)
    # a fleet conversation nobody typed into
    add("r2_a0_w1", "user", "autonomous goal", now - 50)
    add("r2_a0_w1", "assistant", "autonomous answer", now - 40)
    # a follow-up from long before the cut-off
    add("r3_a0_w2", "user", marker + "old", 1_000_000_000)
    add("r3_a0_w2", "assistant", "old answer", 1_000_000_010)
    conn.close()
    spec = importlib.util.spec_from_file_location(
        "backfill_chat_sessions", os.path.join(REPO, "scripts", "backfill_chat_sessions.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return S, mod, store_dir


def _count(S, table):
    conn = S._db(import_files=False)
    try:
        return conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
    finally:
        conn.close()


def test_a_dry_run_writes_nothing_and_counts(synth, capsys):
    S, mod, _d = synth
    assert mod.main(["--since", "2026-01-01"]) == 0
    out = capsys.readouterr().out
    assert "conversations to add: 1 (turns: 4)" in out
    assert "dry run" in out
    assert _count(S, "sessions") == 0 and _count(S, "turns") == 0


def test_apply_writes_one_session_in_one_pass_and_a_second_run_adds_nothing(synth, capsys):
    S, mod, _d = synth
    assert mod.main(["--apply", "--since", "2026-01-01", "--min-free-gb", "0"]) == 0
    assert "written: 1 conversations, 4 turns" in capsys.readouterr().out
    assert _count(S, "sessions") == 1 and _count(S, "turns") == 4
    conn = S._db(import_files=False)
    try:
        row = conn.execute("SELECT source, turns, status FROM sessions").fetchone()
        roles = [r[0] for r in conn.execute("SELECT role FROM turns ORDER BY turn")]
    finally:
        conn.close()
    assert tuple(row) == ("fleet-chat", 4, "done")
    assert roles == ["user", "assistant", "user", "assistant"]
    assert mod.main(["--apply", "--since", "2026-01-01", "--min-free-gb", "0"]) == 0
    out = capsys.readouterr().out
    assert "conversations to add: 0" in out and "already present: 1" in out
    assert _count(S, "sessions") == 1 and _count(S, "turns") == 4


def test_it_prints_counts_only(synth, capsys):
    _S, mod, _d = synth
    mod.main(["--apply", "--since", "2026-01-01", "--min-free-gb", "0"])
    out = capsys.readouterr().out
    assert SECRET_GOAL not in out and SECRET_LINE not in out


def test_it_refuses_when_the_drive_is_too_full(synth, capsys):
    S, mod, _d = synth
    assert mod.main(["--apply", "--since", "2026-01-01", "--min-free-gb", "100000000"]) == 2
    assert "refused" in capsys.readouterr().out
    assert _count(S, "sessions") == 0


def test_it_refuses_when_the_database_is_locked(synth, capsys, monkeypatch):
    S, mod, _d = synth
    holder = sqlite3.connect(S._db_path(), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    real = sqlite3.connect

    def quick(*a, **k):
        k["timeout"] = 0.2
        return real(*a, **k)
    monkeypatch.setattr(sqlite3, "connect", quick)
    try:
        assert mod.main(["--apply", "--since", "2026-01-01", "--min-free-gb", "0"]) == 2
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert "refused" in capsys.readouterr().out
    monkeypatch.setattr(sqlite3, "connect", real)
    assert _count(S, "sessions") == 0


def test_a_failure_midway_rolls_the_whole_batch_back(synth, monkeypatch):
    S, mod, _d = synth
    planned = [("s0101000000aaaa", "k", [("user", "q", 1.0), ("assistant", "a", 2.0)], "g"),
               ("s0101000000aaaa", "k2", [("user", "q", 1.0)], "g")]    # same sid: PK violation
    conn = S._db(import_files=False)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            mod._write(conn, planned)
    finally:
        conn.close()
    assert _count(S, "sessions") == 0 and _count(S, "turns") == 0
