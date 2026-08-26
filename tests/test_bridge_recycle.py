"""会話がトークン上限に達したとき、ブリッジも新しい会話へ移ることを固定する。

同じ検知と復帰は relay と fleet worker には入っていて、ブリッジにだけ無かった。
しかもブリッジは1つの会話に一番長く書き足す側で、実作業に加えて10分ごとの
死活確認（1日144回）が同じ会話に積もる。上限に達すると以降のすべての応答が
同じエラーになり、そのまま返し続けていた。

ブリッジ本体は Playwright とページスレッドを前提にしていて import できないので、
検知と回数上限の振る舞いだけを取り出して確かめる。
"""
import importlib.util
import os

import pytest

from relay.copilot_autopilot_relay import conversation_exhausted

BRIDGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "bridge", "copilot_bridge.py")


def _source():
    with open(BRIDGE, encoding="utf-8") as f:
        return f.read()


def test_exhaustion_is_detected_in_the_forms_copilot_actually_returns():
    assert conversation_exhausted("OpenAIModelTokenLimit")
    assert conversation_exhausted("このスレッドはトークンの上限に達しました")
    assert conversation_exhausted("maximum context length exceeded")


def test_the_ContextTokenLimitExceeded_code_counts_too():
    """The same condition under a code this detector did not know.

    A worker hit it on 2026-08-26 and was not recycled: the reply read as prose that merely
    lacked a DONE, so it was nudged to continue -- into a conversation with no room for the
    nudge -- six times, and was then recorded as `no DONE after 6 continue nudges`. That
    reads as an agent that would not finish. It was a full conversation, and ten turns of
    real findings were discarded with it.

    Verbatim, because the whole failure was a form nobody had matched against.
    """
    assert conversation_exhausted(
        "エラーが発生しました。\n"
        "エラー コード: ContextTokenLimitExceeded\n"
        "会話 ID: d25424be-891c-43a5-a84f-52304378bbc5\n"
        "時間 (UTC): 2026-08-26T03:30:14.146Z。")


def test_the_new_code_does_not_widen_the_net():
    """Matching a code must not start catching answers that merely discuss limits."""
    assert not conversation_exhausted("context について説明します")
    assert not conversation_exhausted("上限は特にありません")


def test_a_normal_answer_that_mentions_tokens_is_not_exhaustion():
    # 「トークン」と書いてあるだけで会話を捨てると、普通の回答で復帰が暴発する
    assert not conversation_exhausted("トークンとは単語の断片のことです")
    assert not conversation_exhausted("63 件見つかりました")
    assert not conversation_exhausted("")


def test_bridge_calls_the_detector_in_its_turn_loop():
    src = _source()
    assert "_bridge_recycle_if_exhausted" in src
    # 検知して終わりではなく、送り直すところまでが復帰
    turn = src[src.index("_bridge_recycle_if_exhausted(final)"):]
    assert "_send_and_stream_once" in turn[:400], "新会話にしただけで送り直していない"


def test_recycling_is_bounded():
    src = _source()
    assert "MAX_BRIDGE_RECYCLES" in src
    # 上限が無いと、会話の長さが原因でない場合に永久に会話を作り続ける
    guard = src[src.index("def _bridge_recycle_if_exhausted"):]
    assert "_BRIDGE_RECYCLES >= MAX_BRIDGE_RECYCLES" in guard[:600]


def test_new_conversation_helper_is_shared_with_the_endpoint():
    """/new と復帰が同じ処理を通ること。

    分けて書くと、片方だけ直して他方が古いままになる。実際その形で、復帰は
    ブリッジにだけ入っていなかった。
    """
    src = _source()
    assert src.count("def _open_fresh_conversation") == 1
    assert "_open_fresh_conversation(title)" in src        # /new から
    assert '_open_fresh_conversation(title="")' in src     # 復帰から
