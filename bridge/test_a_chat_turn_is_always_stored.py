# -*- coding: utf-8 -*-
"""Every chat turn the bridge is asked to keep is in the session store, or the person is told.

THE REPORT (owner, 2026-10-05): chatting from the cockpit window continued after 9/18, and the
`sessions` table has no row after 9/18 -- so a chat that happened is missing from the store.
That is a defect, not "no chats".

WHAT THE CODE DID. `_persist_exchange` wrote the exchange as TWO separate autocommit appends
(`S.append_turn` for the user line, another for the reply) inside `except Exception:
logger.warning(...)`. A locked file, a full disk or an invalid session id left the ledger short
and said so on a line of bridge.log. The window drew the answer as though it had been kept. A
turn that raised or came back empty recorded NOTHING, not even what the person typed.

WHAT IT DOES NOW, and what this file holds it to:

  * session_store.record_exchange: the user line, the reply and the session counter in ONE
    transaction -- all or nothing -- and idempotent (expect_turn / recent-duplicate check);
  * _record_exchange_durably: retried on a transient failure, spilled to a second file when it
    still fails, logged at ERROR, and returned as (False, reason) -- never swallowed;
  * the /stream handler turns that result into a `persist` / `persist_error` event, which the
    window shows as one line ("この会話は保存されていません: <reason>").

Run against a temporary store; nothing here touches the operator's real .fleet.
"""
from __future__ import annotations

import io
import json
import os
import re
import sqlite3

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A throwaway store and a throwaway spill file, via the same env var the writers read."""
    monkeypatch.setenv("MCP_SESSION_STORE_DIR", str(tmp_path / "sessions"))
    from bridge import session_store as S
    from bridge import copilot_bridge as B
    monkeypatch.setattr(B, "PERSIST_FAILURES_PATH", str(tmp_path / "chat_persist_failures.jsonl"))
    for k in list(B._CHAT_PERSIST):
        monkeypatch.setitem(B._CHAT_PERSIST, k, 0)
    monkeypatch.setattr(B, "_PERSIST_RETRY_DELAYS", (0, 0))
    return S, B


def _turns(S, sid):
    conn = S._db()
    try:
        return [(r["turn"], r["role"], r["text"]) for r in conn.execute(
            "SELECT turn, role, text FROM turns WHERE sid = ? ORDER BY turn", (sid,))]
    finally:
        conn.close()


# ── the transaction ──────────────────────────────────────────────────────────────────────────

def test_n_exchanges_land_as_2n_numbered_rows(store):
    S, _B = store
    sid = S.new_session()["sid"]
    for i in range(1, 8):
        assert S.record_exchange(sid, "q%d" % i, "a%d" % i) == 2
    rows = _turns(S, sid)
    assert [r[0] for r in rows] == list(range(1, 15))
    assert [r[1] for r in rows] == ["user", "assistant"] * 7
    assert S.load(sid)["turns"] == 14


def test_a_user_line_with_no_answer_is_kept(store):
    S, _B = store
    sid = S.new_session()["sid"]
    assert S.record_exchange(sid, "typed, never answered", "") == 1
    assert _turns(S, sid) == [(1, "user", "typed, never answered")]


def test_the_same_exchange_twice_is_recorded_once(store):
    S, _B = store
    sid = S.new_session()["sid"]
    assert S.record_exchange(sid, "q", "a") == 2
    assert S.record_exchange(sid, "q", "a") == 0          # a retried write
    assert len(_turns(S, sid)) == 2
    # and by turn number: a replay of turn 1 writes nothing, a new number writes
    assert S.record_exchange(sid, "q", "a", expect_turn=1) == 0
    assert S.record_exchange(sid, "q2", "a2", expect_turn=3) == 2
    assert [r[0] for r in _turns(S, sid)] == [1, 2, 3, 4]


def test_a_session_that_does_not_exist_yet_is_created_by_the_write(store):
    S, _B = store
    sid = "s0101000000abcd"
    assert S.load(sid) is None
    assert S.record_exchange(sid, "hello", "hi") == 2
    assert S.load(sid)["turns"] == 2


def test_a_failure_halfway_leaves_nothing_behind(store, monkeypatch):
    """ALL OR NOTHING. The old shape could leave the user line without its reply, or the rows
    without the counter. Fail the session-counter write, after both turn rows went in."""
    S, _B = store
    sid = S.new_session()["sid"]

    def boom(*a, **k):
        raise sqlite3.OperationalError("database or disk is full")
    monkeypatch.setattr(S, "_write_session", boom)
    with pytest.raises(sqlite3.OperationalError):
        S.record_exchange(sid, "q", "a")
    monkeypatch.undo()
    assert _turns(S, sid) == []


def test_an_invalid_session_id_raises_instead_of_vanishing(store):
    S, _B = store
    with pytest.raises(ValueError):
        S.record_exchange("not a sid", "q", "a")


# ── the bridge's wrapper: every route ends in it ──────────────────────────────────────────────

def test_every_chat_route_stores_through_persist_exchange(store, monkeypatch):
    """/stream, the /goal work loop and the queue drain all call `_persist_exchange`; one
    function, so one test per route-shape is a test of the same writer with that route's
    arguments (a plain turn, a goal turn, a drained queued item)."""
    S, B = store
    monkeypatch.setattr(B, "socket_conv_ref", lambda: "")
    monkeypatch.setattr(B, "_capture_changed_conv_ref", lambda: "")
    monkeypatch.setattr(B, "register_bridge_session_in_fleet_convs", lambda *a, **k: None)
    sid = S.new_session()["sid"]
    routes = [("stream", "hello", "world"),
              ("goal", "do the thing", "did the thing DONE"),
              ("drain", "queued steer", "ok")]
    for _name, user, answer in routes:
        ok, reason = B._persist_exchange(sid, user, answer)
        assert ok is True and reason == ""
    rows = _turns(S, sid)
    assert [(r[1], r[2]) for r in rows] == [
        ("user", "hello"), ("assistant", "world"),
        ("user", "do the thing"), ("assistant", "did the thing DONE"),
        ("user", "queued steer"), ("assistant", "ok")]
    check = B._chat_persist_selfcheck()
    assert check["asked"] == check["written"] == 3 and check["ok"] is True


def test_a_turn_with_no_answer_still_keeps_what_was_typed(store):
    S, B = store
    sid = S.new_session()["sid"]
    assert B._record_exchange_durably(sid, "unanswered", "") == (True, "")
    assert _turns(S, sid) == [(1, "user", "unanswered")]


def test_a_transient_failure_is_retried_and_then_lands(store, monkeypatch):
    S, B = store
    sid = S.new_session()["sid"]
    real = S.record_exchange
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)
    monkeypatch.setattr(S, "record_exchange", flaky)
    assert B._record_exchange_durably(sid, "q", "a") == (True, "")
    assert calls["n"] == 3 and len(_turns(S, sid)) == 2


@pytest.mark.parametrize("message, word", [
    ("database or disk is full", "disk full"),
    ("attempt to write a readonly database", "not writable"),
    ("database is locked", "locked"),
])
def test_a_store_that_refuses_is_reported_spilled_and_counted(store, monkeypatch, tmp_path,
                                                              message, word):
    """DISK FULL / READ-ONLY / LOCKED, simulated at the store boundary. The result is a reason
    the window can show; the words are in the spill file; the counts disagree."""
    S, B = store
    sid = S.new_session()["sid"]

    def refuse(*a, **k):
        raise sqlite3.OperationalError(message)
    monkeypatch.setattr(S, "record_exchange", refuse)
    ok, reason = B._record_exchange_durably(sid, "the question", "the answer")
    assert ok is False and word in reason
    spilled = [json.loads(l) for l in io.open(B.PERSIST_FAILURES_PATH, encoding="utf-8")]
    assert len(spilled) == 1
    assert spilled[0]["user"] == "the question" and spilled[0]["assistant"] == "the answer"
    check = B._chat_persist_selfcheck()
    assert check["asked"] == 1 and check["written"] == 0 and check["failed"] == 1
    assert check["ok"] is False


def test_a_real_readonly_database_file_is_reported(store, tmp_path, monkeypatch):
    """Not a stub: make the sqlite file itself refuse writes (a read-only connection is what
    `?mode=ro` gives), through the real _connect."""
    S, B = store
    sid = S.new_session()["sid"]
    real_connect = S._connect

    def ro_connect():
        conn = sqlite3.connect("file:%s?mode=ro" % S._db_path().replace("\\", "/"), uri=True,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn
    monkeypatch.setattr(S, "_connect", ro_connect)
    monkeypatch.setattr(S, "_initialize", lambda conn: None)
    ok, reason = B._record_exchange_durably(sid, "q", "a")
    monkeypatch.setattr(S, "_connect", real_connect)
    assert ok is False and reason
    assert _turns(S, sid) == []       # nothing half-written, and the store reads again
    assert os.path.isfile(B.PERSIST_FAILURES_PATH)


# ── the handler tells the window ─────────────────────────────────────────────────────────────

def _handler(B):
    h = object.__new__(B.Handler)
    sent = []
    h._sse = lambda data, event=None: sent.append((data, event))
    return h, sent


def test_the_stream_says_persist_ok_when_it_landed(store):
    _S, B = store
    B._CHAT_PERSIST.update(asked=1, written=1, failed=0)
    h, sent = _handler(B)
    h._sse_persist_result(True, "")
    assert sent[0][0]["persist"] == "ok" and sent[0][0]["persist_written"] == 1


def test_the_stream_says_persist_error_with_the_reason_when_it_did_not(store):
    _S, B = store
    B._CHAT_PERSIST.update(asked=2, written=1, failed=1)
    h, sent = _handler(B)
    h._sse_persist_result(False, "disk full (x)")
    d = sent[0][0]
    assert d["persist_error"] == "disk full (x)" and d["persist_asked"] == 2 \
        and d["persist_failed"] == 1


# ── nothing in the persist path may swallow ──────────────────────────────────────────────────

def _func_source(path, name, indent=""):
    text = io.open(os.path.join(REPO, path), encoding="utf-8").read()
    m = re.search(r"\n%sdef %s\(.*?(?=\n%s(?:def |class |@)|\Z)" % (indent, re.escape(name), indent),
                  text, re.S)
    assert m, "%s not found in %s" % (name, path)
    return m.group(0)


SWALLOW = re.compile(r"except\s*(?:Exception|BaseException)?\s*(?:as\s+\w+)?\s*:\s*\n?\s*pass\b")


@pytest.mark.parametrize("path, name", [
    ("bridge/session_store.py", "record_exchange"),
    ("bridge/copilot_bridge.py", "_record_exchange_durably"),
    ("bridge/copilot_bridge.py", "_persist_exchange"),
    ("bridge/copilot_bridge.py", "_spill_unstored_exchange"),
])
def test_the_persist_functions_do_not_swallow_an_exception(path, name):
    src = _func_source(path, name)
    assert not SWALLOW.search(src), "%s swallows an exception with `pass`" % name
    assert not re.search(r"except\s*:", src), "%s has a bare except" % name


def test_the_stream_handler_reports_the_persist_result_on_every_path():
    src = _func_source("bridge/copilot_bridge.py", "_stream_text", "    ")
    assert src.count("_sse_persist_result") >= 2, \
        "both the answered turn and the failed/empty turn must tell the window"
    assert "_record_exchange_durably(sid, msg" in src, \
        "a turn that raised or came back empty must still keep what was typed"
