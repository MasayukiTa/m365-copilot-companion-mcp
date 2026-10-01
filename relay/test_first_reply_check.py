# -*- coding: utf-8 -*-
"""The first message of a fresh conversation must be verified as absorbed, and sent again once
it demonstrably was not.

Fixtures are real replies, shortened only where they were long, taken from the stored fleet
transcripts (the first assistant reply of workers whose conversation had no goal) and from the
first replies that were real work. A false "not absorbed" costs a duplicate send, so the
negatives matter as much as the positives.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import first_reply_check as C            # noqa: E402
import relay.relay_fleet as RF                      # noqa: E402
from relay import mechanism_telemetry as MT         # noqa: E402

GOAL = "C:\\work\\repo\\relay\\relay_fleet.py の goal_ledger を監査して結果を報告する。"

#: Replies from an agent that never received the goal (real, from .fleet/transcripts).
NOT_ABSORBED = {
    "greeting_offer": "こんにちは、武内さん。ご用件をどうぞ。何かお手伝いできることはありますか?",
    "greeting_word": "Greeting をどうぞ。何かお手伝いできることはありますか?",
    "greeting_bare": "Greeting",
    "ask_goal": "実行したい目標の内容を教えてください。",
    "ask_edit": "どのファイルに対してどのような編集を適用したいか、具体的な編集内容を教えてください。",
    "empty_msg": "Greeting ではなさそうですが、メッセージが空のようです。ご用件を書いていただけますか？\n"
                 "what would you like me to help with?",
    "no_instruction": "特に指示がないようです。何かお手伝いできることはありますか？\n"
                      "（メッセージが空でしたので、ご用件をお知らせください。）",
    "offer_list": "予定表やメール、ファイル操作など、何かお手伝いできることはありますか？"
                  "ご用件をお知らせください。",
    "canned": "申し訳ございません。それに応答できませんでした。他に何かお手伝いできることはありますか?",
    "canned_en": "I couldn't respond to that. Is there anything else I can help with?",
    "no_escalation": "現在、このコパイロットでは担当者へのエスカレーションが構成されていません。",
}

#: First replies that ARE work, or at least are not a goal-less agent.
ABSORBED = {
    "done_short": "結果: 4\n\nDONE",
    "done_number": "169\n\nDONE",
    "tool_call": 'call_tool(name="unlock", arguments={"password":"<redacted>"})',
    "close_intent": "9\n\nCloseIntentToolを呼び出します。",
    "next_conf": "NEXT: クラス1を対象ディレクトリで grep。\nCONFIDENCE: medium\n\n次ターンで確認します。",
    "continue": "起動。次ターンで done を確認します。\n\nCONTINUE",
    "split_verdict": "この作業は分割すべきでない。\n\n理由: 単一のPPTXファイルを共有し、並列に書き換えると衝突する。",
    "no_skill_work": "一致するスキルはありません。通常手順で進めます。対象画像を目視分析します。",
    "captcha_refusal": "申し訳ありませんが、CAPTCHAに含まれる文字の読み取りには協力できません。"
                       "画像内の文字以外であれば、お手伝いできます。",
    "platform_error": "エラーが発生しました。\nエラー コード: GenAIToolPlannerRateLimitReached\n会話 ID: 0a",
    "done_with_help_word": "完了。ファイルを作成しました。他にお手伝いできることがあれば言ってください。\n\nDONE",
    "verdict_fail": "FAIL: 実行すべき書込タスクが指定されておらず、解錠が不要。",
    "long": "監査結果をまとめる。" + "欠陥を確認した。" * 60,
    "code_fence": "修正はこうです。\n```python\nx = 1\n```",
}


@pytest.mark.parametrize("name", sorted(NOT_ABSORBED))
def test_a_goalless_agents_reply_is_not_absorbed(name):
    ok, why = C.first_reply_absorbed(NOT_ABSORBED[name], GOAL)
    assert ok is False, (name, why)
    assert why


@pytest.mark.parametrize("name", sorted(ABSORBED))
def test_a_reply_that_shows_work_is_left_alone(name):
    ok, why = C.first_reply_absorbed(ABSORBED[name], GOAL)
    assert ok is True, (name, why)


def test_the_reason_names_the_pattern():
    assert C.first_reply_absorbed(NOT_ABSORBED["empty_msg"], GOAL)[1] == "empty_message"
    assert C.first_reply_absorbed(NOT_ABSORBED["canned"], GOAL)[1] == "canned_refusal"


def test_a_short_reply_that_echoes_the_goal_has_read_it():
    ok, why = C.first_reply_absorbed("goal_ledger を読みます。何かお手伝いできることがあれば後で。", GOAL)
    assert (ok, why) == (True, "echoes_goal")


def test_a_goal_that_is_itself_a_greeting_is_answered_with_one():
    ok, why = C.first_reply_absorbed("こんにちは。ご用件をどうぞ。", "こんにちは")
    assert ok, why


def test_no_reply_is_not_judged_and_nothing_raises():
    assert C.first_reply_absorbed("", GOAL)[0] is True
    assert C.first_reply_absorbed(None, None)[0] is True
    assert C.first_reply_absorbed(12345, object())[0] is True


def test_strong_reasons_are_a_subset_of_what_the_checker_can_return():
    assert set(C.STRONG_ABSORBED_REASONS) == {"work_evidence", "long_reply", "echoes_goal"}


# ── the worker ────────────────────────────────────────────────────────────────────────────

class _Tx:
    def __init__(self):
        self.metrics = []

    def metric(self, turn, name, value, **kw):
        self.metrics.append((turn, name, value, kw))

    def assistant(self, *a, **k):
        pass


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(MT, "LOG", str(tmp_path / "mechanisms.jsonl"))
    return lambda: [r for r in MT.load(str(tmp_path / "mechanisms.jsonl"))
                    if r["mechanism"] == "first_message_not_absorbed"]


def _worker():
    w = RF.RelayWorker(GOAL, "w0", fanout=False)
    w._tx = _Tx()
    w.turn = 1                      # the first message has been sent
    return w


def _first(w):
    return w._first_message


def test_the_worker_keeps_the_real_first_message():
    w = _worker()
    assert GOAL in _first(w)
    assert w.job == _first(w)


def test_a_greeting_redelivers_the_full_first_message_and_records_it(ledger):
    w = _worker()
    w._note_first_reply(NOT_ABSORBED["ask_goal"])
    assert w._first_reply_gate(NOT_ABSORBED["ask_goal"]) is True
    assert w.status == "ready"
    assert w.job.endswith(_first(w))
    lead = w.job[:len(w.job) - len(_first(w))]
    assert lead == RF.RelayWorker.FIRST_MESSAGE_LEAD_IN and lead.count("\n") == 2   # one line
    assert w._first_redeliveries == 1
    rows = ledger()
    assert [r["mechanism"] for r in rows] == ["first_message_not_absorbed"]
    assert rows[0]["extra"]["reason"] == "asks_for_goal" and rows[0]["extra"]["redelivery"] == 1
    assert w._tx.metrics[-1][1] == "first_message_redelivery"


def test_an_absorbed_first_reply_is_untouched(ledger):
    w = _worker()
    before = w.job
    w._note_first_reply(ABSORBED["tool_call"])
    assert w._first_reply_gate(ABSORBED["tool_call"]) is False
    assert w._first_absorbed is True
    assert w.job == before and w._first_redeliveries == 0 and ledger() == []


def test_a_short_unremarkable_reply_stops_the_watch_without_a_send(ledger):
    w = _worker()
    w._note_first_reply("了解しました。")
    assert w._first_reply_gate("了解しました。") is False
    assert w._first_absorbed is True and ledger() == []


def test_once_absorbed_a_later_greeting_is_never_redelivered(ledger):
    """Idempotence: the watch is about the FIRST message only."""
    w = _worker()
    w._note_first_reply(ABSORBED["done_short"])
    w._first_reply_gate(ABSORBED["done_short"])
    w._note_first_reply(NOT_ABSORBED["greeting_offer"])
    assert w._first_reply_gate(NOT_ABSORBED["greeting_offer"]) is False
    assert w._first_redeliveries == 0 and ledger() == []


def test_redelivery_is_bounded_and_then_the_worker_ends_with_an_explicit_outcome(ledger):
    w = _worker()
    for n in (1, 2):
        w._note_first_reply(NOT_ABSORBED["greeting_offer"])
        assert w._first_reply_gate(NOT_ABSORBED["greeting_offer"]) is True
        assert w.status == "ready" and w._first_redeliveries == n
    w._note_first_reply(NOT_ABSORBED["greeting_offer"])
    assert w._first_reply_gate(NOT_ABSORBED["greeting_offer"]) is True
    assert w.status == "stuck" and w.outcome == "INFRA_STUCK"
    assert "再送" in w.reason and w._first_redeliveries == 2
    assert len(ledger()) == 2


def test_until_absorbed_the_nudge_is_the_first_message_not_the_ledger():
    """The incident: turn 1 got no reply, the retry nudge pointed at a message never acted on."""
    w = _worker()
    job = w._task_anchor("もう一度")
    assert job.endswith(_first(w)) and "最初のメッセージ" not in job.replace(
        RF.RelayWorker.FIRST_MESSAGE_LEAD_IN, "")
    assert w._first_redeliveries == 1


def test_nudge_redelivery_shares_the_bound_and_then_falls_back_to_the_ledger():
    w = _worker()
    w._task_anchor("a")
    w._task_anchor("b")
    third = w._task_anchor("c")
    assert w._first_redeliveries == 2
    assert _first(w) not in third and third.endswith("c")


def test_after_absorption_the_nudge_is_the_compact_ledger():
    w = _worker()
    w._note_first_reply(ABSORBED["done_short"])
    job = w._task_anchor("続けて")
    assert _first(w) not in job and "最初のメッセージ" in job and job.endswith("続けて")


def test_before_the_first_send_the_anchor_is_untouched():
    w = RF.RelayWorker(GOAL, "w0", fanout=False)
    assert w.turn == 0
    assert "最初のメッセージ" in w._task_anchor("n")
    assert w._first_redeliveries == 0


def test_a_resumed_conversation_has_no_first_message_to_repeat():
    w = RF.RelayWorker({"text": GOAL, "resume_conv": "https://example.invalid/chats/abc"}, "w0",
                       fanout=False)
    w.turn = 1
    assert w._first_message is None
    assert w._first_message_redelivery("nudge") == ""
    w._note_first_reply(NOT_ABSORBED["greeting_offer"])
    assert w._first_reply_gate(NOT_ABSORBED["greeting_offer"]) is False


def test_a_platform_error_does_not_end_the_watch():
    w = _worker()
    w._note_first_reply(ABSORBED["platform_error"])
    assert w._first_reply_gate(ABSORBED["platform_error"]) is False
    assert w._first_absorbed is False


def test_the_mechanism_is_registered():
    assert "first_message_not_absorbed" in MT.MECHANISMS


def test_decide_wires_the_gate_end_to_end(ledger):
    """Through _decide itself: a goal-less greeting re-arms the worker, the next work reply
    is left alone."""
    w = _worker()
    w.status = "waiting"
    w._decide(NOT_ABSORBED["greeting_offer"])
    assert w.status == "ready" and w.job.endswith(_first(w))
    w.status = "waiting"
    w._decide("ファイルを確認しました。\n\nCONTINUE")
    assert w._first_absorbed is True
    assert w.job is not None and _first(w) not in w.job
    assert w._first_redeliveries == 1
