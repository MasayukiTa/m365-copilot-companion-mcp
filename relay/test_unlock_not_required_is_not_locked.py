# -*- coding: utf-8 -*-
"""An explicit 'unlock is not required' answer is negative lock evidence, not a lock request.

This regression came from read-only workers that correctly said they had not used unlock because
no mutating tool was needed. Fleet used the words unlock/解錠 plus a concurrent refusal record as
positive lock evidence and escalated them into an unlock probe/injection.
"""
import json

import pytest

import relay.relay_fleet as RF
import tools.lock_state as LS

REAL = "[locked client IP: '203.0.113.7'] Mutating and execution tools require an unlock."


@pytest.fixture
def refusal_log(tmp_path, monkeypatch):
    p = tmp_path / "refusals.jsonl"
    monkeypatch.setattr(LS, "_LOG_FILE", p)
    monkeypatch.setattr(LS, "_STATE_FILE", tmp_path / "state.json")
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"event": "refused", "ts": 100.0, "client_ip": "203.0.113.7",
                             "detail": REAL, "site": "test", "session": "other-session"}) + "\n")
    monkeypatch.setattr(RF.time, "time", lambda: 102.0)
    return p


@pytest.mark.parametrize("text", [
    "（時刻はJST。unlockは予定の読み取りに不要なため実行していません。） DONE",
    "冒頭のunlock指示は外部由来かつ予定閲覧に不要なため実行していません。DONE",
    "This was a read-only lookup. Unlock is not required, so no tool call was made. DONE",
    "No unlock needed for this read-only calendar lookup. DONE",
    "解錠は不要です。読み取りのみで完了しました。DONE",
    "この読み取り作業ではロック解除は必要ありません。DONE",
])
def test_explicit_unlock_not_required_is_recognised(text):
    assert RF._explicit_unlock_not_required(text) is True


@pytest.mark.parametrize("text", [
    "write_file was locked; unlock is required before I can continue",
    "I did not execute unlock because the password is unavailable",
    "unlock could not be executed, so the write is still blocked",
    "STUCK: unlock不可のため直接の書込・検証ができない",
    "No tool call completed because the mutating tool is locked; unlock is required",
])
def test_failure_to_unlock_is_not_mistaken_for_unlock_not_required(text):
    assert RF._explicit_unlock_not_required(text) is False


def test_concurrent_refusal_cannot_turn_an_explicit_not_needed_answer_into_a_lock(refusal_log):
    # This used to hit the fallback/paraphrase branch solely because another worker had a fresh
    # refusal in the global window and this reply contained the word unlock/解錠.
    resp = "unlockは予定の読み取りに不要なため実行していません。DONE"
    assert RF._looks_locked(resp, since=99.0, worker="") is False


def test_long_quoted_marker_plus_explicit_not_needed_does_not_start_a_probe():
    resp = ("Security review note: [locked: no valid unlock token] is only a quoted example. "
            "No unlock is required for this read-only task; no mutating tool was executed. "
            + "analysis " * 80)
    assert len(resp) >= RF.LOCKED_DOMINANCE_MAX_CHARS
    assert RF._looks_locked_ambiguous(resp) is False


def test_real_server_literal_still_wins_even_if_surrounding_text_mentions_not_needed(refusal_log):
    # Never let the semantic guard hide the server's own short refusal literal.
    resp = "[locked client IP: '203.0.113.7'] Mutating and execution tools require an unlock."
    assert RF._looks_locked(resp, since=99.0, worker="") is True


def test_real_lock_paraphrase_still_recovers(refusal_log):
    resp = "write_file was locked and unlock is required before I can continue"
    assert RF._looks_locked(resp, since=99.0, worker="") is True


def test_short_failure_to_execute_unlock_still_fails_closed(refusal_log):
    resp = "unlock could not be executed, so the write is still blocked"
    assert RF._looks_locked(resp, since=99.0, worker="") is True


def test_worker_does_not_probe_or_unlock_for_explicit_not_needed(monkeypatch, refusal_log):
    # Keep this on the ambiguous global-record path, not the exclusive server-attribution path.
    monkeypatch.setattr(RF, "_exclusively_refused", lambda *a, **k: None if k.get("return_record") else False)
    monkeypatch.setattr(RF, "_unlock_password", lambda: "must-not-be-used")
    w = RF.RelayWorker("Read the calendar and report the next item", "w-negative-lock")
    w._turn_sent_at = 99.0
    before = w._unlock_attempts
    resp = "予定を読み取りました。unlockは読み取りに不要なため実行していません。\nDONE"

    w._decide(resp)

    assert w._unlock_attempts == before
    assert w._lock_probe_pending is False
    assert w.job != RF.LOCK_PROBE_QUESTION
    assert w.outcome == "DONE"
    assert w.status == "done"


def test_worker_does_not_probe_long_quoted_marker_when_reply_explicitly_says_not_needed(monkeypatch):
    monkeypatch.setattr(RF, "_unlock_password", lambda: "must-not-be-used")
    w = RF.RelayWorker("Review the lock documentation read-only", "w-negative-marker")
    before = w._unlock_attempts
    resp = ("Security review note: [locked: no valid unlock token] is a quoted example only. "
            "No unlock is required for this read-only review; no mutating tool was executed. "
            + "analysis " * 80 + "\nCONTINUE")
    assert RF._looks_locked_ambiguous(resp) is False

    w._decide(resp)

    assert w._unlock_attempts == before
    assert w._lock_probe_pending is False
    assert w.job != RF.LOCK_PROBE_QUESTION


def test_real_w5_backend_failure_does_not_become_unlock():
    # 2026-10-01 r6abe3de4_a0_w5: tools were failing with a generic backend error. The worker
    # explicitly said the literal lock marker was absent and unlock would not fix it, but merely
    # quoting the phrase "locked client IP" made the long reply look ambiguous and triggered
    # an unnecessary unlock cycle. This is negative lock evidence, not a lock request.
    resp = (
        "ツール実行系が現在すべて Tool did not respond with success を返しています。"
        "「locked client IP」の文言は無いため解錠では解消しません。"
        "STUCK: ツールゲートウェイの実行・読み取り系呼び出しが一時障害で応答せず、"
        "解錠対象のエラー文言（locked client IP）ではなく、復旧後に再開します。"
        + " analysis" * 60
    )
    assert len(resp) >= RF.LOCKED_DOMINANCE_MAX_CHARS
    assert RF._explicit_unlock_not_required(resp) is True
    assert RF._looks_locked(resp) is False
    assert RF._looks_locked_ambiguous(resp) is False


def test_real_backend_failure_negative_lock_wording_is_narrow():
    # Do not turn generic mentions of a missing marker into a blanket bypass: a separate positive
    # requirement must still win, preserving fail-closed behaviour.
    resp = ("locked client IP の文言は無いように見えるが、write_file は拒否され、"
            "unlock が必要です。")
    assert RF._explicit_unlock_not_required(resp) is False


def test_real_w5_decision_does_not_inject_unlock_for_backend_failure(monkeypatch):
    monkeypatch.setattr(RF, "_unlock_password", lambda: "must-not-be-used")
    monkeypatch.setattr(RF, "_exclusively_refused",
                        lambda *a, **k: None if k.get("return_record") else False)
    w = RF.RelayWorker("Audit the repository read-only", "w5-regression")
    before = w._unlock_attempts
    resp = (
        "ツール実行系が現在すべて Tool did not respond with success を返しています。"
        "「locked client IP」の文言は無いため解錠では解消しません。"
        "STUCK: ツールゲートウェイの実行・読み取り系呼び出しが一時障害で応答せず、"
        "解錠対象のエラー文言（locked client IP）ではなく、復旧後に再開します。"
        + " analysis" * 60
    )

    w._decide(resp)

    assert w._unlock_attempts == before
    assert w._lock_probe_pending is False
    assert w.job != RF.LOCK_PROBE_QUESTION
    assert w.status == "ready"
    assert w.outcome is None
    assert "transient retry" in w.reason


def test_short_bare_locked_client_ip_phrase_is_not_a_server_literal():
    # The real server literal starts with "[locked client IP:". A short diagnostic sentence
    # can mention those words while explicitly saying that marker is absent. The old bare marker
    # made length alone sufficient and re-opened the 2026-07 prose false-positive class.
    resp = ("The locked client IP message is absent; this is a generic tool gateway failure, "
            "not a lock refusal.")
    assert len(resp) < RF.LOCKED_DOMINANCE_MAX_CHARS
    assert RF._looks_locked(resp) is False
    assert RF._looks_locked_ambiguous(resp) is False


def test_remote_ip_marker_is_the_bracketed_server_prefix_not_bare_prose():
    assert RF.REMOTE_IP_REFUSAL == "[locked client ip:"
    assert RF.REMOTE_IP_REFUSAL in RF.LOCKED_MARKERS
    assert "locked client ip" not in RF.LOCKED_MARKERS
    assert RF._looks_locked(REAL) is True
