# -*- coding: utf-8 -*-
"""A platform error reply (Copilot's structured 'エラー コード: <Code>') is classified from ONE
table (relay/platform_errors.py) and answered by class, not left to the generic text handlers.

Real strings: the SystemError reply is from .fleet/transcripts/r6ad0b77d_a0_w8.jsonl turn 1
(2026-10-15, the worker that then sat in 'waiting'); ContextTokenLimitExceeded is the
126-character reply measured in 262 of 263 occurrences. AsyncResponsePayloadTooLarge has no
stored transcript on this machine: it is written in the same service format.
"""
from __future__ import annotations

import pytest

import relay.relay_fleet as F
from relay import platform_errors as PE

SYSTEM_ERROR = ("申し訳ありません。予期しないエラーが発生しました。現在調査中しています。"
                "エラー コード: SystemError。会話 ID: f54bbaae-d52a-42bf-8336-62f445363bbf。"
                "時刻 (UTC): 10/15/2026 11:24:13 AM。")
CONTEXT_LIMIT = ("エラーが発生しました。\n"
                 "エラー コード: ContextTokenLimitExceeded\n"
                 "会話 ID: ae789ed4-f7b5-4b1b-9ec7-80bdf1dbb044\n"
                 "時間 (UTC): 2026-08-31T00:32:49.073Z。")
PAYLOAD = CONTEXT_LIMIT.replace("ContextTokenLimitExceeded", "AsyncResponsePayloadTooLarge")
ENGLISH = "Sorry, an unexpected error occurred. Error code: SystemError. Conversation ID: abc"
UNKNOWN = CONTEXT_LIMIT.replace("ContextTokenLimitExceeded", "BrandNewFaultCode")
GOAL = "テスト用のゴール"


# --- the table -----------------------------------------------------------------------------
@pytest.mark.parametrize("text,code,action", [
    (SYSTEM_ERROR, "SystemError", PE.TRANSIENT),
    (ENGLISH, "SystemError", PE.TRANSIENT),
    (CONTEXT_LIMIT, "ContextTokenLimitExceeded", PE.CONTEXT),
    (PAYLOAD, "AsyncResponsePayloadTooLarge", PE.CONTEXT),
    (CONTEXT_LIMIT.replace("ContextTokenLimitExceeded", "RequestBodyTooLarge"),
     "RequestBodyTooLarge", PE.CONTEXT),
    (CONTEXT_LIMIT.replace("ContextTokenLimitExceeded", "GatewayTimeout"),
     "GatewayTimeout", PE.TRANSIENT),
    (CONTEXT_LIMIT.replace("ContextTokenLimitExceeded", "GenAIToolPlannerRateLimitReached"),
     "GenAIToolPlannerRateLimitReached", PE.PASS),
    (CONTEXT_LIMIT.replace("ContextTokenLimitExceeded", "AgentBlocked"), "AgentBlocked", PE.PASS),
    (UNKNOWN, "BrandNewFaultCode", PE.UNKNOWN),
])
def test_table_classification(text, code, action):
    err = PE.detect(text)
    assert err is not None and (err.code, err.action) == (code, action)


def test_ordinary_and_long_replies_are_not_platform_errors():
    assert PE.detect("ウィンドウを6件確認しました。次に進みます。") is None
    assert PE.detect("") is None
    analysis = "所見を続けます。" * 200 + " エラー コード: SystemError と出ています。"
    assert len(analysis) > PE.MAX_CHARS and PE.detect(analysis) is None


# --- the worker ----------------------------------------------------------------------------
@pytest.fixture()
def worker(monkeypatch):
    monkeypatch.setattr(F, "_unlock_password", lambda: "pw-placeholder-not-a-credential")
    monkeypatch.setattr(F, "connector_proof_source", lambda: "run")
    monkeypatch.setattr(F, "default_notify", lambda *a, **k: None)
    w = F.RelayWorker(GOAL, "w0")
    w._last_sent_job = "FIRST MESSAGE"
    return w


def _decide(w, resp, turn=1):
    w.turn = turn
    w.status = "waiting"
    w._decide(resp)


def test_system_error_is_a_fresh_conversation_with_the_same_goal(worker, monkeypatch):
    monkeypatch.setattr(F.time, "time", lambda: 1000.0)
    delays = []
    for n in (1, 2, 3):
        _decide(worker, SYSTEM_ERROR, worker._fresh_turn_base + 1)
        assert worker.status == "ready" and worker.outcome in ("", None)
        assert worker.reason == "一時的なプラットフォームエラー(SystemError)→会話を作り直し(%d/3)" % n
        assert worker._platform_recycles == n
        assert worker._needs_socket_rebind and worker._socket_conv_id == ""
        delays.append(worker._cooldown_until - 1000.0)
    assert delays == [5.0, 15.0, 30.0]
    assert worker.job == "FIRST MESSAGE"                      # verbatim, not reworded
    assert worker._canned_recycles == 0 and worker._recycles == 0   # separate budgets


def test_after_the_bound_the_known_transient_ride_out_applies_and_is_not_left_waiting(
        worker, monkeypatch):
    monkeypatch.setattr(F.time, "time", lambda: 1000.0)
    worker._platform_recycles = F.FRESH_RETRY_MAX
    _decide(worker, SYSTEM_ERROR, 1)
    assert worker.status == "ready"          # the live defect: it stayed 'waiting' for ever
    assert worker._copilot_err_streak == 1
    assert worker._cooldown_until > 1000.0


def test_unknown_code_one_retry_then_stuck_with_the_code_first(worker, monkeypatch):
    monkeypatch.setattr(F.time, "time", lambda: 1000.0)
    _decide(worker, UNKNOWN, 1)
    assert worker.status == "ready"
    assert worker.reason == "未知のプラットフォームエラー(BrandNewFaultCode)→会話を作り直し(1/1)"
    _decide(worker, UNKNOWN, worker._fresh_turn_base + 1)
    assert worker.status == "stuck" and worker.outcome == "INFRA_STUCK"
    assert worker.reason.startswith("プラットフォームエラー: BrandNewFaultCode")
    assert worker.stuck_cause == "platform"
    assert worker.stuck_cause_info["code"] == "BrandNewFaultCode"


@pytest.mark.parametrize("text", [CONTEXT_LIMIT, PAYLOAD])
def test_context_and_payload_codes_take_the_token_limit_recycle(worker, monkeypatch, text):
    class _Route:
        def driver_for(self, name, turn_timeout_s=None):
            return object()

    class _Drv:
        def close(self):
            pass

    monkeypatch.setattr(F, "_socket_route", lambda: _Route())
    worker.socket, worker.drv = True, _Drv()
    _decide(worker, text, 1)
    assert worker.status == "ready" and worker.outcome in ("", None)
    assert worker._recycles == 1 and worker._platform_recycles == 0
    assert worker.reason.startswith("会話トークン上限 → 新会話で続行")
    assert GOAL in worker.job                                 # goal re-anchored


def test_rate_limit_code_is_left_to_the_throttle_path(worker):
    text = CONTEXT_LIMIT.replace("ContextTokenLimitExceeded", "GenAIToolPlannerRateLimitReached")
    _decide(worker, text, 1)
    assert worker._platform_recycles == 0 and worker._recycles == 0


def test_stuck_cause_platform_is_in_the_vocabulary():
    from relay import stuck_cause as sc
    c = sc.platform_cause(code="SystemError", attempts=3)
    assert "platform" in sc.KINDS and c.headline().startswith("プラットフォームエラー: SystemError")
    assert sc.already_stamped(c.headline())


# --- reviewer gets the matched procedure's own text ----------------------------------------
def test_reviewer_prompt_carries_the_bounded_procedure_body(worker):
    from relay import refuter
    worker._skill_proc = {"name": "some-skill", "description": "does a thing",
                          "body": "STEP-ONE\n" + "x" * (F.PROCEDURE_BODY_MAX * 2)}
    note = worker._procedure_note()
    assert note.startswith("some-skill: does a thing") and "STEP-ONE" in note
    assert len(note) < 600 + F.PROCEDURE_BODY_MAX + 100
    prompt = refuter.build_refuter_prompt(GOAL, "報告", procedure=note)
    assert "STEP-ONE" in prompt and "手順本文" in prompt


def test_procedure_note_without_a_body_is_the_one_line_identity(worker):
    worker._skill_proc = {"name": "some-skill", "description": "does a thing"}
    assert worker._procedure_note() == "some-skill: does a thing"
