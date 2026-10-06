# -*- coding: utf-8 -*-
"""Timing and attribution fields on the tool-call ledger.

The rules held here: a call row and its outcome carry start/end stamps that stay comparable
across a restart; a worker's identity is taken only from what it declared itself through the
turn-loop protocol on the same session, and a call with no such declaration stays empty.
"""
import json

import pytest

from tools import tool_ledger as L


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    path = str(tmp_path / "tool_events.jsonl")
    monkeypatch.setattr(L, "LEDGER_PATH", path, raising=False)
    L._SESSION_IDENTITY.clear()
    return path


def rows(path):
    return [json.loads(x) for x in open(path, encoding="utf-8").read().splitlines() if x.strip()]


def with_session(monkeypatch, sid):
    monkeypatch.setattr(L, "session_fingerprint", lambda: sid)


def test_call_and_outcome_carry_start_end_and_monotonic_stamps(ledger):
    cid = L.record_call("read_file", {"path": "a"})
    L.record_outcome(cid, ok=True, result="x")
    call, out = rows(ledger)
    assert call["proc"] == out["proc"] and call["proc"]
    assert isinstance(call["mono"], float) and out["mono"] >= call["mono"]
    assert out["ts_start"] == call["ts"] and out["ts_end"] == out["ts"]
    assert out["dur_mono_s"] >= 0
    # the existing fields are untouched
    assert {"schema", "event", "id", "ts", "tool", "task", "worker", "turn", "args"} <= set(call)
    assert "duration_s" in out


def test_explicit_task_and_worker_are_kept_and_marked(ledger):
    L.record_call("x", {}, task="t1", worker="w1")
    r = rows(ledger)[0]
    assert (r["task"], r["worker"], r["attr"]) == ("t1", "w1", "explicit")


def test_no_session_declaration_leaves_task_and_worker_empty(ledger, monkeypatch):
    with_session(monkeypatch, "s1")
    L.record_call("read_file", {"path": "a"})
    r = rows(ledger)[0]
    assert r["task"] == "" and r["worker"] == "" and "attr" not in r


def test_identity_is_learned_from_the_workers_own_claim(ledger, monkeypatch):
    with_session(monkeypatch, "s1")
    L.record_call("claim_turn", {"job_id": "job7", "expected_seq": 1, "worker_id": "w3"})
    L.record_call("read_file", {"path": "a"})
    with_session(monkeypatch, "s2")
    L.record_call("read_file", {"path": "b"})          # another session: not attributed
    a, b, c = rows(ledger)
    assert (a["task"], a["worker"]) == ("job7", "w3")
    assert (b["task"], b["worker"], b["attr"]) == ("job7", "w3", "session")
    assert c["task"] == "" and c["worker"] == ""


def test_commit_ends_the_attribution(ledger, monkeypatch):
    with_session(monkeypatch, "s1")
    L.record_call("claim_turn", {"job_id": "job7", "worker_id": "w3"})
    L.record_call("commit_turn", {"job_id": "job7"})
    L.record_call("read_file", {"path": "a"})
    _, commit, after = rows(ledger)
    assert commit["task"] == "job7" and commit["worker"] == "w3"
    assert after["task"] == "" and after["worker"] == ""


def test_a_second_job_on_the_session_does_not_inherit_the_old_worker(ledger, monkeypatch):
    with_session(monkeypatch, "s1")
    L.record_call("claim_turn", {"job_id": "job7", "worker_id": "w3"})
    L.record_call("heartbeat", {"job_id": "job8"})
    assert rows(ledger)[1]["worker"] == ""
