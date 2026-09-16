# -*- coding: utf-8 -*-
"""100 workers were recycled for writing about token limits, and 81 had already said DONE.

`conversation_exhausted` decides whether Copilot has told us the conversation is full. When
it says yes, relay/relay_fleet.py:4659 discards the worker's entire conversation and starts
a new one. Its docstring said the rule was "kept conservative so a normal answer that merely
discusses tokens does not false-fire". That was true of three of its four rules and false of
the only one that had ever fired.

MEASURED over all 2,318 transcripts on this machine, 5,783 assistant rows:

    contexttokenlimitexceeded    263 matches, 262 of them EXACTLY 126 characters
    トークン + 上限/制限/超え     103 matches, median 3,141 characters, ZERO genuine
    openaimodeltokenlimit          0 matches
    maximum context length         0 matches

100 of the 102 Japanese-only matches were recycled. 81 of those discarded replies ended in
DONE. An entire fan-out investigating this repo's own unlock-token expiry was destroyed for
using the words トークン and 上限 -- the 上限 being the 8-token cap in .unlock_state.json.

The strings below are the real ones, from the runs named in each test. This is the same
two-part rule -- distinctive marker AND dominance -- that tools/tool_ledger.py::looks_refused
and relay/relay_fleet.py::_looks_locked already use, reached from the same kind of incident.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.copilot_autopilot_relay import (  # noqa: E402
    EXHAUSTED_DOMINANCE_MAX_CHARS,
    conversation_exhausted,
)

#: The genuine error, 262 of 263 occurrences identical modulo the UUID and timestamp.
GENUINE = ("エラーが発生しました。\n"
           "エラー コード: ContextTokenLimitExceeded\n"
           "会話 ID: ae789ed4-f7b5-4b1b-9ec7-80bdf1dbb044\n"
           "時間 (UTC): 2026-08-31T00:32:49.073Z。")


def test_the_only_error_ever_measured_is_still_detected():
    assert conversation_exhausted(GENUINE)
    assert len(GENUINE) < EXHAUSTED_DOMINANCE_MAX_CHARS, (
        "the bound is now tighter than the real error, so nothing would ever be detected")


def test_the_bound_leaves_room_over_the_only_error_ever_measured():
    """126 characters is the whole observed range -- shortest and longest are the same.
    The bound is not tuned to that; it is the constant this repo already uses for the same
    judgement, which leaves 3.2x headroom for a form nobody here has seen."""
    assert EXHAUSTED_DOMINANCE_MAX_CHARS >= 3 * len(GENUINE)


@pytest.mark.parametrize("text,run", [
    # The unlock-token fan-out. r6aa0995a alone lost 63 of these.
    ("## 報告：unlockトークンの保持場所と失効条件（担当範囲 1/6）\n"
     ".unlock_state.json はIP単位でトークンのハッシュを保持し、上限は8個です。\n"
     + "詳細は以下のとおりです。" * 200,
     "r6aa0995a_a0_w113 (10,391 chars, recycled)"),
    ("MCP_REQUIRE_UNLOCK_TOKEN=1（トークン強制）が有効化されたことを確認しました。"
     "保持数の上限に関する記述は次のとおりです。" + "確認しました。" * 300,
     "r6a9eaee5_a0_w24 (5,847 chars, recycled)"),
    # Shorter than the genuine error. No length bound could have saved the old rule.
    ("次のスライドへ進みます。トークン制限を避け、1枚ずつ処理します。 CONTINUE",
     "r6aa787c0_a0_w53 (71 chars, recycled)"),
    ("スライド2〜4を見ます。トークン制限のため1枚ずつ確認します。 CONTINUE",
     "r6aa787c0_a0_w35 (108 chars, recycled)"),
])
def test_a_worker_writing_about_token_limits_is_not_a_full_conversation(text, run):
    assert not conversation_exhausted(text), run


def test_quoting_the_error_code_in_an_analysis_is_not_the_error():
    """r6a9eaee5_a0_w16 turn 11: a worker read a transcript, quoted the code in 2,652
    characters of analysis, and was recycled for it. This is what dominance is for."""
    analysis = ("transcript を読みました。" + "所見を続けます。" * 300
                + " 末尾に ContextTokenLimitExceeded と出ています。")
    assert len(analysis) > EXHAUSTED_DOMINANCE_MAX_CHARS
    assert not conversation_exhausted(analysis)


@pytest.mark.parametrize("text", [
    "このスレッドはトークンの上限に達しました",
    "コンテキストの上限に達しました。新しい会話を開始してください。",
    "OpenAIModelTokenLimit",
    "Error: maximum context length exceeded.",
])
def test_the_forms_that_have_never_been_seen_here_are_still_claimed(text):
    """None of these has occurred in 2,318 transcripts. They are a hypothesis about other
    locales and builds, which is what they always were -- the difference is that the
    Japanese one is now a phrase somebody would only write if it had happened, rather than
    two nouns that this codebase's own subject matter puts in the same paragraph."""
    assert conversation_exhausted(text)


def test_the_old_vocabulary_rule_has_not_come_back():
    """Two nouns in one paragraph, which is what 102 destroyed conversations looked like."""
    assert not conversation_exhausted("トークンの話をします。上限について説明します。")
    assert not conversation_exhausted("トークンは制限されています。")
    assert not conversation_exhausted("トークン数が超えることがあります。")


def test_nothing_and_rubbish_are_not_a_full_conversation():
    for value in ("", None, "   ", "DONE"):
        assert not conversation_exhausted(value)
