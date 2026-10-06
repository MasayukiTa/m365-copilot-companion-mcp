# -*- coding: utf-8 -*-
"""The reviewer's own conversation, and the string the worker really received, are in sqlite.

WHAT WAS MISSING (owner's report, 2026-10-06: "the full text recorded in sqlite is required,
system prompt included -- no record of the refutation is not acceptable"):

  * The refuter / review panel left only a verdict KIND and a <=300 character reason
    (panels.jsonl / mechanisms.jsonl). What the reviewer was sent, and everything it answered,
    was stored nowhere.
  * The worker's `user` turn in fleet_turns is the job text as COMPOSED. On the socket route the
    transport prepends a protocol preamble and the tool catalogue (relay/chathub.Conversation.ask
    -> socket_tools.build_prompt); on the tab route send() collapses whitespace to one line.
    The string the agent actually received was in neither the row nor any file.

WHAT THESE TESTS DO. They run the real functions -- the store writers, RefuterSession.poll()
through a nudge to a verdict, RelayWorker._poll_refute, chathub.Conversation.ask, the socket
driver's turn thread, _Transcript.wire -- against a throwaway store, and read the rows back.
Nothing here asserts on source text.
"""
import hashlib
import json
import os
import sqlite3
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from bridge import session_store as S  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv(S.STORE_DIR_ENV, str(tmp_path))
    monkeypatch.delenv(S.FULLTEXT_MAX_CHARS_ENV, raising=False)
    return tmp_path


def _rows(db_dir, sql, args=()):
    conn = sqlite3.connect(os.path.join(str(db_dir), "sessions.sqlite3"))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


# ------------------------------------------------------------------ the store writer itself

def test_a_full_text_is_stored_whole_and_a_rerun_adds_nothing(store):
    text = "line one\n\n" + ("x" * 50_000) + "\nREFUTED: the tests were not run"
    for _ in range(3):                                     # the same exchange, written again
        assert S.record_refuter_turn("k1", "refuter_user", 0, text, lens="security",
                                     name="w1", run_id="r1") is True
    rows = S.fleet_turns(key=S.refuter_key("k1", "security"))
    assert len(rows) == 1
    r = rows[0]
    assert r["text"] == text                               # whole, not cut
    x = r["extra"]
    assert x["truncated"] is False and x["orig_chars"] == len(text)
    assert x["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert x["sha16"] == x["sha256"][:16] == x["pre_redaction_sha16"]   # nothing was redacted
    assert (x["run_id"], r["name"], x["lens"], r["role"]) == ("r1", "w1", "security",
                                                              "refuter_user")


def test_a_different_text_at_the_same_position_is_kept_not_dropped(store):
    S.record_refuter_turn("k1", "refuter_assistant", 1, "UPHELD")
    S.record_refuter_turn("k1", "refuter_assistant", 1, "REFUTED: no")
    rows = S.fleet_turns(key=S.refuter_key("k1"))
    assert sorted(r["text"] for r in rows) == ["REFUTED: no", "UPHELD"]


def test_a_secret_never_reaches_the_row(store, monkeypatch):
    secret = "canary-FULLTEXT-5d2c91ab77"
    monkeypatch.setenv("FULLTEXT_TEST_API_TOKEN", secret)
    S.record_refuter_turn("k1", "refuter_user", 0, "use %s to log in" % secret,
                          extra={"note": "also %s" % secret})
    S.record_wire_turn("k1", 1, "wire carries %s" % secret)
    blob = json.dumps(_rows(store, "SELECT * FROM fleet_turns"))
    assert secret not in blob
    assert "REDACT" in blob.upper()


def test_a_failed_redactor_withholds_the_text(store, monkeypatch):
    import tools.secret_store as ss

    def boom(*a, **k):
        raise RuntimeError("redactor down")
    monkeypatch.setattr(ss, "secret_values", boom)
    S.record_refuter_turn("k1", "refuter_user", 0, "secret-bearing text")
    [row] = S.fleet_turns(key=S.refuter_key("k1"))
    assert row["text"] == "[redaction failed: content withheld]"


def test_over_the_cap_the_cut_is_recorded_not_silent(store, monkeypatch, caplog):
    monkeypatch.setenv(S.FULLTEXT_MAX_CHARS_ENV, "100")
    text = "y" * 450
    S.record_refuter_turn("k1", "refuter_user", 0, text)
    [row] = S.fleet_turns(key=S.refuter_key("k1"))
    x = row["extra"]
    assert x["truncated"] is True and x["orig_chars"] == 450 and len(row["text"]) == 100
    assert x["sha256"] == hashlib.sha256(text.encode()).hexdigest()   # of the FULL text
    assert any("exceeds" in r.getMessage() for r in caplog.records)


def test_a_failed_write_is_logged_not_swallowed(store, monkeypatch, caplog):
    def broken(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(S, "_db", broken)
    assert S.record_refuter_turn("k1", "refuter_user", 0, "t") is False
    assert S.record_fleet_turn("k1", {"role": "user", "text": "t"}) is False
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "record_refuter_turn failed" in msgs and "OperationalError" in msgs
    assert "record_fleet_turn failed" in msgs


def test_existing_rows_and_schema_are_untouched(store):
    S.record_fleet_turn("old", {"role": "user", "text": "kept", "turn": 1}, name="w")
    S.record_refuter_turn("old", "refuter_user", 0, "review")
    assert [r["text"] for r in S.fleet_turns(key="old")] == ["kept"]
    tables = {r["name"] for r in _rows(store, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "llm_full_text" not in tables               # no new table: fleet_turns roles only


# ------------------------------------------------------------------ the reviewer, end to end

class _Answers:
    def __init__(self, n):
        self._n = n

    def count(self):
        return self._n


class _FakeReviewerDriver:
    """Stands in for the reviewer's page driver: records what is sent, answers on request."""

    def __init__(self, replies):
        self.sent, self._replies, self._count = [], list(replies), 0
        self._text = ""

    def send(self, text, **kw):
        self.sent.append(text)
        self._count += 1
        self._text = self._replies.pop(0)

    def _answers(self):
        return _Answers(self._count)

    def read_last_response(self):
        return self._text

    def close(self):
        pass


def _worker(store_key="w1_a0", name="w1"):
    from relay.relay_fleet import RelayWorker
    w = RelayWorker.__new__(RelayWorker)
    w._tx_key, w.name, w.run_id = store_key, name, "run-77"
    w.refute_count, w.turn, w.review_lenses = 1, 3, []
    w.fresh_replay_count = 0
    w._settle_done = lambda: None
    return w


def test_a_review_is_stored_in_full_through_a_nudge_to_the_verdict(store):
    from relay.refuter import RefuterSession, build_refuter_prompt
    w = _worker()
    goal, final = "fix the parser\nand add a test", "I fixed it.\nDONE"
    sess = RefuterSession(None, "https://agent.example/x", goal, final, dwell_s=0.0,
                          recorder=w._refuter_recorder("")).start()
    sess.drv = _FakeReviewerDriver(["I will check the files first.",
                                    "REFUTED: no test was added for the parser"])
    sess._pending_open = False
    sess._send_prompt()                                    # what _do_open does after the page

    polls = 0
    w._refuter_session = sess
    while polls < 20:                                      # the fleet's own loop, via the worker
        polls += 1
        if w._poll_refute() is not False or w._refuter_session is None:
            break
    assert w._refuter_session is None, "the review never reached a verdict"
    assert w._last_refute_verdict == "REFUTED"

    expected_prompt = build_refuter_prompt(goal, final, lens="", unverifiable=False)
    rows = S.fleet_turns(key=S.refuter_key("w1_a0"), limit=100)
    by_role = {}
    for r in sorted(rows, key=lambda r: r["turn"]):
        by_role.setdefault(r["role"], []).append(r["text"])
    assert by_role["refuter_user"][0] == expected_prompt   # the WHOLE prompt, newlines and all
    assert len(by_role["refuter_user"]) == 2               # prompt + the nudge
    assert by_role["refuter_user"][1] == sess.drv.sent[1]
    assert by_role["refuter_assistant"] == ["I will check the files first.",
                                            "REFUTED: no test was added for the parser"]
    assert by_role["refuter_verdict"] == ["REFUTED: no test was added for the parser"]
    assert {(r["extra"]["run_id"], r["name"]) for r in rows} == {("run-77", "w1")}
    first = [r for r in rows if r["role"] == "refuter_user"][0]["extra"]
    assert first["preamble_id"] and first["route"] == "tab"
    assert all(r["extra"]["sha16"] == hashlib.sha256(r["text"].encode()).hexdigest()[:16]
               for r in rows)

    # THE SAME FLUSH AGAIN ADDS NOTHING: replay every exchange through the recorder.
    before = len(S.fleet_turns(key=S.refuter_key("w1_a0"), limit=1000))
    rec = w._refuter_recorder("")
    for ex in sess.exchanges:
        rec(ex)
    assert len(S.fleet_turns(key=S.refuter_key("w1_a0"), limit=1000)) == before


def test_a_panel_lens_is_recorded_per_reviewer(store):
    from relay.refuter import RefuterSession
    w = _worker()
    for lens in ("security", "edge"):
        sess = RefuterSession(None, "u", "g", "f", dwell_s=0.0, lens=lens,
                              recorder=w._refuter_recorder(lens)).start()
        sess.drv = _FakeReviewerDriver(["UPHELD"])
        sess._send_prompt()
    for lens in ("security", "edge"):
        [row] = S.fleet_turns(key=S.refuter_key("w1_a0", lens))
        assert row["role"] == "refuter_user" and row["extra"]["lens"] == lens


def test_a_recorder_that_fails_is_reported_and_does_not_stop_the_review(store, capsys):
    from relay.refuter import RefuterSession

    def bad(ex):
        raise OSError("disk full")
    sess = RefuterSession(None, "u", "g", "f", recorder=bad).start()
    sess.drv = _FakeReviewerDriver(["UPHELD"])
    sess._send_prompt()                                    # must not raise
    assert "recorder failed" in capsys.readouterr().err


# ------------------------------------------------------------------ the worker's real wire text

def test_the_socket_payload_with_preamble_and_tools_is_what_is_stored(store):
    from relay import chathub
    from relay.relay_fleet import _Transcript
    from relay.socket_driver import CopilotSocketDriver

    conv = chathub.Conversation(lambda: "tok")
    sent_on_wire = []
    conv._one_exchange = lambda payload, **kw: (sent_on_wire.append(payload) or "an answer")
    catalogue = [{"name": "read_file", "description": "read a file", "parameters": {}}]
    drv = CopilotSocketDriver(conv, connect=None, catalogue=catalogue,
                              protocol="PROTOCOL-PREAMBLE\n")

    w = _worker()
    w.socket, w.drv, w.turn = True, drv, 0
    w._tx = _Transcript(None, "w1_a0", "w1", "the goal")
    w._install_wire_sink()
    drv.send("the job text")
    assert drv.wait_for_idle(timeout_s=10)

    assert len(sent_on_wire) == 1
    wire_rows = [r for r in S.fleet_turns(key="w1_a0") if r["role"] == "user_wire"]
    assert len(wire_rows) == 1
    row = wire_rows[0]
    assert row["text"] == sent_on_wire[0]                  # byte for byte what went out
    assert row["text"].startswith("PROTOCOL-PREAMBLE") and "<tools>" in row["text"]
    assert row["text"].endswith("the job text")
    assert row["turn"] == 1
    assert row["extra"]["route"] == "socket" and row["extra"]["truncated"] is False
    assert row["extra"]["sha256"] == hashlib.sha256(row["text"].encode()).hexdigest()


def test_the_tab_wire_text_is_the_collapsed_single_line(store):
    from relay.relay_fleet import _Transcript
    tx = _Transcript(None, "w2_a0", "w2", "g")
    job = "first line\n\n   second   line\n"
    tx.wire(4, " ".join(job.split()), route="tab", run_id="run-1")
    [row] = [r for r in S.fleet_turns(key="w2_a0") if r["role"] == "user_wire"]
    assert row["text"] == "first line second line" and row["extra"]["route"] == "tab"


def test_a_failed_wire_write_is_reported_on_stderr(store, monkeypatch, capsys):
    from relay.relay_fleet import _Transcript
    tx = _Transcript(None, "w3_a0", "w3", "g")
    monkeypatch.setattr(S, "record_wire_turn",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked")))
    tx.wire(1, "text")                                     # must not raise
    assert "was NOT recorded" in capsys.readouterr().err
