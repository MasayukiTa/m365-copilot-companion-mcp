# -*- coding: utf-8 -*-
"""The refuter had nowhere to put "this information does not exist".

MEASURED on the 290-cinema survey of 2026-09-04. A worker was sent back three times in a row,
answered 調べ尽くした each time, and was told to try PDF parsing and proxied fetches. Its final
conclusion was materially the same as its first: three turns of quota spent on pressure alone.

The mechanism is in the prompt. REFUTER_INSTRUCTION is written for code -- it names 境界値,
例外処理, セキュリティ and tells the reviewer to open 実物のファイルやテスト -- and a survey has
neither. Then it tells the reviewer to hunt 全力で for a reason the goal was not met, while the
three verdicts leave no place for a well-supported negative finding. A reviewer under those
instructions can always say "you did not try X".

Same shape as the benchmark-side finding of the same day: a gate whose predicate cannot be
satisfied discards everything and teaches nothing.

WHAT MUST NOT CHANGE. The same write-up credits this reviewer with catching real sloppiness --
one worker checked 1 of 9 subjects, another reused a different campaign's figure, another
processed the wrong subject entirely. Those are defects in the SEARCH and they stay refutable.
Only "I searched properly and there is nothing" becomes an acceptable answer.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from relay import refuter as R  # noqa: E402

GOAL = "全国290館について先着特典の配布状況を調べて"
FOUND_NOTHING = "9館中3館は公式サイト・検索とも一次情報に到達できず、情報なしとしました。"


def test_a_coding_goal_gets_exactly_the_prompt_it_always_got():
    """The default must not move a byte, or this is a behaviour change wearing a fix's clothes."""
    plain = R.build_refuter_prompt(GOAL, FOUND_NOTHING)
    assert plain.startswith(R.REFUTER_INSTRUCTION)
    assert R.UNVERIFIABLE_PREAMBLE not in plain


def test_an_unverifiable_goal_is_told_a_negative_finding_is_legitimate():
    got = R.build_refuter_prompt(GOAL, FOUND_NOTHING, unverifiable=True)
    assert R.UNVERIFIABLE_PREAMBLE in got
    assert "情報が存在しない" in got and "正当な結論" in got


def test_it_redirects_the_question_from_the_answer_to_the_search():
    got = R.build_refuter_prompt(GOAL, FOUND_NOTHING, unverifiable=True)
    assert "調べ方が十分だったか" in got


def test_merely_thinking_of_another_method_is_ruled_out_as_a_reason():
    """THE MEASURED FAILURE: PDF parsing and proxied fetches, demanded three times."""
    got = R.build_refuter_prompt(GOAL, FOUND_NOTHING, unverifiable=True)
    assert "追加の手段を思いつくというだけでは" in got


def test_repeating_the_same_objection_is_ruled_out():
    """Three consecutive send-backs produced materially the same conclusion each time."""
    got = R.build_refuter_prompt(GOAL, FOUND_NOTHING, unverifiable=True)
    assert "同じ指摘を繰り返さないでください" in got


@pytest.mark.parametrize("laziness", [
    "対象の一部しか実際には調べていない",
    "参照したと書いてある情報源を実際には開いていない",
    "別の対象・別の項目の情報を流用して判定している",
    "報告された対象名と根拠が食い違っている",
])
def test_the_sloppiness_this_reviewer_actually_caught_stays_refutable(laziness):
    """Each of these is a real case from the survey. Excusing them would trade one failure
    for a worse one."""
    got = R.build_refuter_prompt(GOAL, FOUND_NOTHING, unverifiable=True)
    assert laziness in got


def test_the_original_instruction_is_still_present_underneath():
    """The reframing is a preamble, not a replacement: the reviewer is still adversarial."""
    got = R.build_refuter_prompt(GOAL, FOUND_NOTHING, unverifiable=True)
    assert R.REFUTER_INSTRUCTION in got


def test_a_lens_still_applies_on_top():
    got = R.build_refuter_prompt(GOAL, FOUND_NOTHING, lens="rootcause", unverifiable=True)
    assert R.LENS_PROMPTS["rootcause"] in got and R.UNVERIFIABLE_PREAMBLE in got


def test_the_goal_and_the_report_still_reach_the_reviewer():
    got = R.build_refuter_prompt(GOAL, FOUND_NOTHING, unverifiable=True)
    assert GOAL in got and FOUND_NOTHING in got


def test_the_session_carries_the_flag_to_the_prompt():
    """Wiring, not just the string: the fleet builds RefuterSession, and a flag that never
    reaches the send is a fix that exists only in a unit test."""
    s = R.RefuterSession(None, "https://m365.cloud.microsoft/chat/agent/x", GOAL,
                         FOUND_NOTHING, unverifiable=True)
    assert s.unverifiable is True
    s2 = R.RefuterSession(None, "https://m365.cloud.microsoft/chat/agent/x", GOAL, FOUND_NOTHING)
    assert s2.unverifiable is False
