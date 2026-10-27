# -*- coding: utf-8 -*-
"""A canned no-answer on the first turns, and a tool call printed as text, are faults of the
CONVERSATION. The harness answers them with a NEW conversation and the SAME goal text (never
reworded), bounded, before it files the worker as REFUSED / INFRA_STUCK.

Real reply strings from .fleet/transcripts/r6ad070a3_a0_w3.jsonl (turn 1, canned) and
r6ad070a3_a0_w5.jsonl (turn 2, call written as text), 2026-10-15.
"""
from __future__ import annotations

import pytest

import relay.relay_fleet as F

CANNED = "申し訳ございません。それに応答できませんでした。他に何かお手伝いできることはありますか?"
CANNED_LONG = "ご質問にお答えできませんでした/" + CANNED
MALFORMED = (
    "メモを読み終えました。「検査件数」の対象テーブルを確認します。接続を確認します。\n\ncall\n"
    '<invoke name="260616test-call_tool">\n'
    '<parameter name="arguments">{"name": "odbc_connections"}</parameter>\n'
    '<parameter name="name">call_tool</parameter>\n</invoke>')
PLAIN = "ウィンドウを6件確認しました。次に進みます。"
GOAL = "テスト用のゴール"


@pytest.fixture()
def worker(monkeypatch):
    monkeypatch.setattr(F, "_unlock_password", lambda: "pw-placeholder-not-a-credential")
    monkeypatch.setattr(F, "connector_proof_source", lambda: "run")
    w = F.RelayWorker(GOAL, "w0")
    w._last_sent_job = "FIRST MESSAGE"
    return w


def _decide(w, resp, turn):
    w.turn = turn
    w.status = "waiting"
    w._decide(resp)


# --- detector ------------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    (MALFORMED, True),
    ('call <invoke name="x"><parameter name="arguments">{}</parameter></invoke>', True),
    ("<invoke name='x'>", True),
    ("<function_calls>", True),
    ("call_tool(name='odbc_connections')", True),
    ("  call_tool (  )", True),
    (PLAIN, False),
    (CANNED, False),
    ("", False),
])
def test_textual_tool_call_detector(text, expected):
    assert F._tried_to_call_a_tool(text) is expected


# --- (a) canned no-answer ------------------------------------------------------------------
@pytest.mark.parametrize("canned", [CANNED, CANNED_LONG])
@pytest.mark.parametrize("turn", [1, 2])
def test_canned_early_is_a_fresh_conversation_with_the_same_goal(worker, monkeypatch, canned, turn):
    monkeypatch.setattr(F.time, "time", lambda: 1000.0)
    _decide(worker, canned, turn)
    assert worker.status == "ready" and worker.outcome in ("", None)
    assert worker.reason == "定型の無回答→会話を作り直し(1/3)"
    assert worker._canned_recycles == 1
    assert worker._needs_socket_rebind and worker._socket_conv_id == "" and worker._mcp_conv_fresh
    assert worker._cooldown_until == pytest.approx(1005.0)
    if turn == 1:
        assert worker.job == "FIRST MESSAGE"       # verbatim, not reworded
    else:
        assert GOAL in worker.job                  # goal re-anchored in the fresh chat


def test_canned_is_bounded_then_falls_to_the_existing_diagnostics(worker, monkeypatch):
    monkeypatch.setattr(F.time, "time", lambda: 1000.0)
    delays = []
    for n in (1, 2, 3):
        _decide(worker, CANNED, worker._fresh_turn_base + 1)
        assert worker.reason == "定型の無回答→会話を作り直し(%d/3)" % n
        delays.append(worker._cooldown_until - 1000.0)
    assert delays == [5.0, 15.0, 30.0]
    _decide(worker, CANNED, worker._fresh_turn_base + 1)
    assert worker.status == "stuck" and worker.outcome == "REFUSED"
    assert worker._canned_recycles == 3
    assert worker._tools_recycles == 0 and worker._recycles == 0   # separate budgets


def test_canned_late_in_a_conversation_is_still_recycled(worker, monkeypatch):
    # NEW BEHAVIOUR (brief fix 1): a canned non-answer is re-queued in a fresh conversation on
    # ANY turn, not only the first two. The old gate `(turn - base) <= CANNED_RETRY_MAX_TURN`
    # filed a turn-3 non-answer straight to stuck; a read-only goal carries no duplicate-act
    # hazard, so turn position must not decide whether it recovers. (explain.md: 直した理由)
    monkeypatch.setattr(F.time, "time", lambda: 1000.0)
    _decide(worker, CANNED, 3)
    assert worker._canned_recycles == 1
    assert worker.status == "ready" and worker.outcome in ("", None)
    assert worker.recovery_state == "recovering"


# --- (b) tool call printed as text ---------------------------------------------------------
def test_malformed_call_gets_one_steer_then_a_fresh_conversation(worker, monkeypatch):
    monkeypatch.setattr(F.time, "time", lambda: 1000.0)
    _decide(worker, MALFORMED, 1)
    assert worker.status == "ready" and worker._malformed_recycles == 0
    assert "ツール呼び出しの仕組み" in worker.job and "call_tool(" not in worker.job
    _decide(worker, MALFORMED, 2)
    assert worker.status == "ready" and worker.outcome in ("", None)
    assert worker._malformed_recycles == 1
    assert worker.reason == "ツール呼び出しがテキストで出力された→会話を作り直し(1/3)"
    assert worker._needs_socket_rebind and worker._mcp_conv_fresh
    assert GOAL in worker.job
    assert worker._cooldown_until == pytest.approx(1005.0)


def test_malformed_is_bounded_then_infra_stuck(worker, monkeypatch):
    monkeypatch.setattr(F.time, "time", lambda: 1000.0)
    delays = []
    for n in (1, 2, 3):
        base = worker._fresh_turn_base
        _decide(worker, MALFORMED, base + 1)       # steer
        _decide(worker, MALFORMED, base + 2)       # repeats -> recycle
        assert worker.reason == "ツール呼び出しがテキストで出力された→会話を作り直し(%d/3)" % n
        delays.append(worker._cooldown_until - 1000.0)
    assert delays == [5.0, 15.0, 30.0]
    base = worker._fresh_turn_base
    _decide(worker, MALFORMED, base + 1)
    _decide(worker, MALFORMED, base + 2)
    assert worker.status == "stuck" and worker.outcome == "INFRA_STUCK"
    assert worker._malformed_recycles == 3 and worker._tools_recycles == 0
    assert worker._recycles == 0


def test_a_clean_reply_after_the_steer_means_no_recycle(worker):
    _decide(worker, MALFORMED, 1)
    _decide(worker, PLAIN, 2)
    assert worker._malformed_recycles == 0 and worker._unlanded_calls == 0


# --- brief fix 1/2/3: canned recovery reads as 自動回復中, not 要対応 ----------------------
# A goal that ACTS on the world, used to check the bounded replay policy. "送信してください" matches ACTING.
ACT_GOAL = "会議の議事録をメールで送信してください"


def test_a_canned_then_a_real_reply_completes_without_needs_attention(worker, monkeypatch):
    # (a) first turn canned -> fresh replay (recovering), second turn a real answer -> the
    # worker is NOT filed as 要対応; the recovering pill drops back to recovered.
    monkeypatch.setattr(F.time, "time", lambda: 1000.0)
    _decide(worker, CANNED, 1)
    assert worker.status == "ready" and worker.recovery_state == "recovering"
    assert worker.outcome in ("", None)          # not stuck, no terminal outcome yet
    _decide(worker, PLAIN, 2)
    assert worker.recovery_state == "recovered"  # cleared the moment a real reply arrives
    assert worker.outcome in ("", None) and worker.status != "stuck"


def test_canned_repeats_until_bound_then_needs_attention(worker, monkeypatch):
    # (b) the SAME canned non-answer coming back again and again spends the bound and only
    # THEN becomes 要対応 (stuck), with the retry count recorded in the reason.
    monkeypatch.setattr(F.time, "time", lambda: 1000.0)
    for n in (1, 2, 3):
        _decide(worker, CANNED, worker._fresh_turn_base + 1)
        assert worker.status == "ready" and worker.recovery_state == "recovering"
    _decide(worker, CANNED, worker._fresh_turn_base + 1)
    assert worker.status == "stuck" and worker.outcome == "REFUSED"
    assert worker._canned_recycles == 3
    assert "3回再投入" in worker.reason          # the reason states how many replays were tried


@pytest.fixture()
def acting_worker(monkeypatch):
    monkeypatch.setattr(F, "_unlock_password", lambda: "pw-placeholder-not-a-credential")
    monkeypatch.setattr(F, "connector_proof_source", lambda: "run")
    monkeypatch.setattr(F.time, "time", lambda: 1000.0)
    monkeypatch.setattr(F._auto_rephrase, "enabled", lambda: True)
    w = F.RelayWorker(ACT_GOAL, "wact")
    w._last_sent_job = "FIRST MESSAGE"
    monkeypatch.setattr(w, "_collect_progress_fields", lambda: ("prior progress", "", ""))
    return w


def test_canned_acting_goal_replays_with_progress_in_fresh_conversation(acting_worker):
    w = acting_worker
    _decide(w, CANNED, 1)
    assert w.status == "ready" and w.outcome in ("", None)
    assert w._acting_rephrase_done is True
    assert w._canned_recycles == 0
    assert w._needs_socket_rebind and w._socket_conv_id == "" and w._mcp_conv_fresh
    assert "prior progress" in w.job and ACT_GOAL in w.job
    assert w.job.index("prior progress") < w.job.index(ACT_GOAL)


def test_second_canned_acting_goal_stops_without_another_replay(acting_worker, monkeypatch):
    w = acting_worker
    calls = []
    start = w._start_fresh_conversation
    def record_start(*args, **kwargs):
        calls.append(args)
        return start(*args, **kwargs)
    monkeypatch.setattr(w, "_start_fresh_conversation", record_start)
    _decide(w, CANNED, 1)
    assert len(calls) == 1 and w._acting_rephrase_done is True
    _decide(w, CANNED, w._fresh_turn_base + 1)
    assert w.status == "stuck" and w.outcome == "REFUSED"
    assert "言い換えを1回試した" in w.reason
    assert len(calls) == 1 and w._canned_recycles == 0


def test_canned_acting_goal_stops_immediately_when_rephrase_disabled(acting_worker, monkeypatch):
    w = acting_worker
    monkeypatch.setattr(F._auto_rephrase, "enabled", lambda: False)
    _decide(w, CANNED, 1)
    assert w.status == "stuck" and w.outcome == "REFUSED"
    assert "再送はしていない" in w.reason
    assert w._acting_rephrase_done is False and w._canned_recycles == 0
    assert not w._needs_socket_rebind
