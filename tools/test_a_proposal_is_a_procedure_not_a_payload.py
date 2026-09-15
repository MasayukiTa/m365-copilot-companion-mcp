# -*- coding: utf-8 -*-
"""What a generated proposal quotes, and what it must not carry forward.

BOTH OF THESE WERE FOUND BY OPENING A GENERATED FILE. 106 proposals were written and the counts
said nothing was wrong; the defects were in the content of one of them.

1. THE QUOTED "INSTRUCTION" WAS NOT ONE. `fleet_goals` stores the text that was SENT, and for a
   fan-out that is the operator's instruction with a slice header appended -- or, on the merge
   turn, with every child's full report pasted after it. The largest proposal's 依頼文 section
   ran to nine subtask reports: colleagues' names, message subjects, per-day counts. Nobody
   typed any of it, and none of it belongs in a procedure.

2. THE INSTRUCTION ITSELF CARRIED A STAFF DIRECTORY. Several hundred colleagues and their mobile
   numbers, pasted into the goal as a reference list. A 19KB draft is one nobody reads and
   therefore nobody approves -- and a Skill is meant to be handed around (the operator's own
   words: send it over Teams if you need to), so a procedure with a directory stapled to it
   travels with one.

The cut for (2) is on LENGTH, not on recognising personal data. Recognising it is the judgement
that cannot be shown to be complete -- the same reason the bundle name is a digest. Length is a
property of the text rather than a guess about it, and an instruction past 1,200 characters has
stopped being a procedure whatever it happens to contain.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools import skill_draft, skill_lessons  # noqa: E402

INSTRUCTION = "四半期の検査結果をまとめて、同じフォルダに出してください。"
SLICE_HEADER = "\n\n【この会話が担当する範囲 — 全体の 2/9】\n(A) 一部だけ担当する。"
MERGE_BLOCK = ("\n\n【分割実行の結果をまとめてください】\n"
               "--- サブタスク 1 / DONE ---\n山田太郎 / 090-0000-0000 / 件名あれこれ")


def test_the_fan_out_header_is_not_part_of_the_instruction():
    got = skill_lessons.operator_instruction(INSTRUCTION + SLICE_HEADER)
    assert got == INSTRUCTION
    assert "担当する範囲" not in got


def test_the_merged_subtask_reports_are_not_part_of_the_instruction():
    got = skill_lessons.operator_instruction(INSTRUCTION + MERGE_BLOCK)
    assert got == INSTRUCTION
    assert "サブタスク" not in got and "090-0000-0000" not in got


def test_a_plain_instruction_survives_untouched():
    """The conservative direction: no marker, nothing removed."""
    assert skill_lessons.operator_instruction(INSTRUCTION) == INSTRUCTION


def test_an_instruction_that_is_only_machinery_is_not_emptied():
    """Returning "" would put a blank 依頼文 section in the draft, which reads as "there was no
    instruction" rather than "this one is all machinery"."""
    only = SLICE_HEADER.strip()
    assert skill_lessons.operator_instruction(only) == only


def test_a_long_instruction_is_cut_and_says_so():
    payload = INSTRUCTION + "".join("氏名%d:090-0000-%04d;" % (i, i) for i in range(400))
    quoted = skill_draft._quote_instruction(payload)
    assert len(quoted) < len(payload)
    assert "省略" in quoted, "the draft cut the instruction without saying it had"
    assert quoted.startswith(INSTRUCTION[:20]), "the cut lost the beginning of the instruction"


def test_a_short_instruction_is_quoted_whole():
    assert skill_draft._quote_instruction(INSTRUCTION) == INSTRUCTION


def test_the_cut_is_on_length_and_not_on_guessing_what_is_sensitive():
    """If the rule were "drop things that look like personal data", this text -- long, and with
    nothing personal in it -- would survive whole, and a short instruction carrying one phone
    number would be cut. Neither is what happens, and that is the point: the rule is one a
    reader can check, not a classifier nobody can audit."""
    harmless_long = "手順です。" + ("同じ作業を繰り返してください。" * 200)
    assert "省略" in skill_draft._quote_instruction(harmless_long)
    short_with_a_number = "山田さん(090-1234-5678)に確認してください。"
    assert skill_draft._quote_instruction(short_with_a_number) == short_with_a_number
