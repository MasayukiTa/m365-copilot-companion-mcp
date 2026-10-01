# -*- coding: utf-8 -*-
"""Coordinator-side attribution: a tool call is labelled from the turn windows the fleet
coordinator wrote (tools/turn_context.py), because real workers never declare their own job.

Synthetic windows only; clock basis is wall-clock epoch seconds, slack 1.5 s per window edge.
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import tool_ledger as L  # noqa: E402
from tools import turn_context as TC  # noqa: E402


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "LEDGER_PATH", str(tmp_path / "tool_events.jsonl"), raising=False)
    monkeypatch.setattr(TC, "CONTEXT_PATH", str(tmp_path / "turn_context.jsonl"), raising=False)
    monkeypatch.setattr(L, "session_fingerprint", lambda: "s1")
    L._SESSION_IDENTITY.clear()
    L._WINDOW_BINDING.clear()
    TC._CACHE.update(key=None, path=None, windows=[])
    return tmp_path


def ledger_rows():
    p = L.LEDGER_PATH
    return [json.loads(x) for x in open(p, encoding="utf-8").read().splitlines() if x.strip()]


def call(ts, session="s1", tool="read_file", **kw):
    L.session_fingerprint = lambda: session
    L.record_call(tool, {"path": "a"}, ts=ts, **kw)
    return ledger_rows()[-1]


def window(worker, job, t0, t1=None, turn=1):
    TC.record_open(worker, job, "run1", turn, t0)
    if t1 is not None:
        TC.record_close(worker, job, "run1", turn, t0, t1)


def test_one_window_attributes_the_call():
    window("w1", "job1", 100.0, 110.0)
    r = call(105.0)
    assert (r["task"], r["worker"], r["attr"]) == ("job1", "w1", "window")


def test_non_overlapping_windows_each_attribute_their_own_calls():
    window("w1", "job1", 100.0, 110.0)
    window("w2", "job2", 120.0, 130.0)
    assert call(105.0, session="a")["worker"] == "w1"
    assert call(125.0, session="b")["worker"] == "w2"
    r = call(115.0, session="c")            # between windows: nobody in flight
    assert r["task"] == "" and "attr" not in r


def test_two_overlapping_workers_are_ambiguous_and_never_guessed():
    window("w1", "job1", 100.0, 120.0)
    window("w2", "job2", 105.0, 125.0)
    r = call(110.0)
    assert r["task"] == "" and r["worker"] == "" and r["attr"] == "ambiguous"


def test_an_earlier_unambiguous_match_binds_the_session():
    window("w1", "job1", 100.0, 120.0)
    window("w2", "job2", 105.0, 125.0)
    assert call(101.0)["attr"] == "window"          # only w1 in flight yet -> bind s1 to w1
    r = call(110.0)                                 # both in flight now
    assert (r["task"], r["worker"], r["attr"]) == ("job1", "w1", "session-window")


def test_a_binding_to_a_worker_not_in_flight_is_not_reused():
    window("w1", "job1", 100.0, 105.0)
    window("w2", "job2", 200.0, 220.0)
    window("w3", "job3", 205.0, 225.0)
    assert call(101.0)["worker"] == "w1"
    r = call(210.0)                                 # w2 and w3 overlap, w1 is long done
    assert r["attr"] == "ambiguous" and r["worker"] == ""


def test_a_new_unambiguous_match_rebinds_the_session():
    window("w1", "job1", 100.0, 105.0)
    window("w2", "job2", 200.0, 230.0, turn=1)
    window("w3", "job3", 210.0, 225.0)
    call(101.0)
    assert call(201.0)["worker"] == "w2"            # rebinds s1 to w2
    assert call(215.0)["worker"] == "w2" and ledger_rows()[-1]["attr"] == "session-window"


def test_explicit_declaration_stays_authoritative():
    window("w1", "job1", 100.0, 110.0)
    r = call(105.0, task="mine", worker="wx")
    assert (r["task"], r["worker"], r["attr"]) == ("mine", "wx", "explicit")


def test_edge_slack_is_one_and_a_half_seconds_each_side():
    window("w1", "job1", 100.0, 110.0)
    assert call(98.6, session="a")["attr"] == "window"      # 1.4 s before the send
    assert "attr" not in call(98.4, session="b")             # 1.6 s before
    assert call(111.4, session="c")["attr"] == "window"     # 1.4 s after the reply
    assert "attr" not in call(111.6, session="d")


def test_touching_windows_of_two_workers_stay_ambiguous_inside_the_slack():
    window("w1", "job1", 100.0, 110.0)
    window("w2", "job2", 110.5, 120.0)
    assert call(110.2)["attr"] == "ambiguous"


def test_same_worker_consecutive_turns_are_one_candidate():
    window("w1", "job1", 100.0, 110.0, turn=1)
    window("w1", "job1", 110.5, 120.0, turn=2)
    r = call(110.2)
    assert r["worker"] == "w1" and r["attr"] == "window"


def test_an_open_turn_claims_calls_until_it_is_abandoned():
    window("w1", "job1", 100.0)                      # never closed
    assert call(500.0, session="a")["attr"] == "window"
    assert "attr" not in call(100.0 + TC.MAX_OPEN_S + 5, session="b")


def test_a_new_send_closes_a_turn_that_never_closed():
    window("w1", "job1", 100.0, None, turn=1)
    window("w1", "job1", 200.0, 210.0, turn=2)
    assert call(150.0, session="a")["worker"] == "w1"       # inside the superseded turn
    assert "attr" not in call(300.0, session="b")           # not claimed for MAX_OPEN_S


def test_job_falls_back_to_the_run_id():
    TC.record_open("w1", "", "runX", 1, 100.0)
    r = call(101.0)
    assert r["task"] == "runX" and r["worker"] == "w1"


def test_missing_file_leaves_everything_empty():
    r = call(100.0)
    assert r["task"] == "" and "attr" not in r


def test_corrupt_lines_cost_only_themselves(env):
    window("w1", "job1", 100.0, 110.0)
    with open(TC.CONTEXT_PATH, "a", encoding="utf-8") as fh:
        fh.write("{not json\n\n[1,2]\n" + json.dumps({"event": "open", "worker": "w9"}) + "\n")
    assert call(105.0)["worker"] == "w1"


def test_declared_session_identity_still_beats_a_window():
    window("w1", "job1", 100.0, 110.0)
    L.session_fingerprint = lambda: "s1"
    L.record_call("claim_turn", {"job_id": "jobD", "worker_id": "wD"}, ts=105.0)
    r = call(106.0)
    assert (r["task"], r["worker"], r["attr"]) == ("jobD", "wD", "session")


def test_the_reader_cache_follows_the_file(env):
    window("w1", "job1", 100.0, 110.0)
    assert call(105.0)["worker"] == "w1"
    window("w2", "job2", 105.0, 112.0)               # file changed: size/mtime differ
    assert call(106.0, session="z")["attr"] == "ambiguous"


def test_the_writer_rotates_at_its_cap_and_keeps_writing(env, monkeypatch):
    monkeypatch.setattr(TC, "WRITE_MAX_BYTES", 600)
    for i in range(20):
        TC.record_open("w%d" % i, "j", "r", 1, 100.0 + i)
    assert os.path.exists(TC.CONTEXT_PATH + ".1")
    assert os.path.getsize(TC.CONTEXT_PATH) < 600 + 400
    rows = [json.loads(x) for x in open(TC.CONTEXT_PATH, encoding="utf-8")]
    assert rows and {"worker", "t_send", "ts", "mono", "proc", "pid"} <= set(rows[0])


def test_the_reader_only_parses_the_tail(env, monkeypatch):
    monkeypatch.setattr(TC, "READ_TAIL_BYTES", 700)
    for i in range(30):
        TC.record_open("old%d" % i, "j", "r", 1, 10.0 + i)
        TC.record_close("old%d" % i, "j", "r", 1, 10.0 + i, 10.5 + i)
    window("w1", "job1", 1000.0, 1010.0)
    assert call(1005.0)["worker"] == "w1"
    assert len(TC._windows()) < 61


def test_the_retention_sweep_caps_this_file_like_the_ledger(tmp_path):
    from relay import fleet_retention
    p = tmp_path / "turn_context.jsonl"
    p.write_text("".join(json.dumps({"event": "open", "worker": "w", "i": i}) + "\n"
                         for i in range(4000)), encoding="utf-8")
    freed, trimmed = fleet_retention.cap_jsonl(str(tmp_path), max_mb=0.05)
    assert "turn_context.jsonl" in trimmed and freed > 0


def test_a_failed_write_never_raises(env, monkeypatch):
    monkeypatch.setattr(TC, "CONTEXT_PATH", os.path.join(str(env), "no", "\0bad"), raising=False)
    TC.record_open("w1", "j", "r", 1, 1.0)
    TC.record_close("w1", "j", "r", 1, 1.0, 2.0)
    TC.record_open("", "j", "r", 1, 1.0)
    TC.record_open("w1", "j", "r", 1, "not-a-number")
