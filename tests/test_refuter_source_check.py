# -*- coding: utf-8 -*-
"""The reviewer must check WHERE an answer came from, not only that it agrees with itself.

MEASURED 2026-10-15. A goal was answered from a spreadsheet while the matched approved procedure
prescribes a different source; the reviewers recomputed the spreadsheet's numbers, found one
internal inconsistency, and (after the worker "fixed" the arithmetic) UPHELD it. The prompt gave
them nothing to compare the answer's source against.

The rule is generic: it names no system, tool or topic -- what a procedure prescribes lives in the
procedure. tests/test_no_domain_words_in_harness.py keeps it that way.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import refuter as R  # noqa: E402

GOAL = "在庫が多い倉庫を上位5つ教えて"
ANSWER = "出典: 共有フォルダの集計表。1位 A倉庫 120 ..."

LENSES = ["", "correctness", "edge", "security", "minimality", "rootcause", "rootcause_code"]
FRAMES = [False, True]


@pytest.mark.parametrize("lens", LENSES)
@pytest.mark.parametrize("unverifiable", FRAMES)
def test_every_prompt_carries_the_source_check(lens, unverifiable):
    got = R.build_refuter_prompt(GOAL, ANSWER, lens=lens, unverifiable=unverifiable)
    assert R.SOURCE_CHECK_RULE in got
    assert "REFUTED" in R.SOURCE_CHECK_RULE and "出典違い" in R.SOURCE_CHECK_RULE


def test_the_default_prompt_still_starts_with_the_instruction():
    assert R.build_refuter_prompt(GOAL, ANSWER).startswith(R.REFUTER_INSTRUCTION)


def test_the_matched_procedure_is_shown_to_the_reviewer_when_there_is_one():
    got = R.build_refuter_prompt(GOAL, ANSWER, procedure="drill: 在庫データの手順")
    assert "--- 一致した承認済み手順 ---\ndrill: 在庫データの手順" in got
    # between the goal and the report, where the reviewer reads both
    assert got.index("--- ゴール ---") < got.index("--- 一致した承認済み手順 ---") < got.index(
        "--- 実装エージェントの最終報告 ---")


@pytest.mark.parametrize("procedure", ["", "   ", None])
def test_no_procedure_adds_no_block(procedure):
    got = R.build_refuter_prompt(GOAL, ANSWER, procedure=procedure)
    assert "一致した承認済み手順 ---" not in got


def test_the_rule_does_not_excuse_a_stated_inability():
    """A worker that says exactly why the prescribed procedure could not be run is not refuted
    for the source -- the failure is then the finding, not the substitute."""
    assert "具体的理由" in R.SOURCE_CHECK_RULE and "出典違いとはしない" in R.SOURCE_CHECK_RULE


def test_the_preamble_id_changes_with_the_rule():
    """Stored review rows say which instruction text the reviewer was given."""
    import hashlib
    old = hashlib.sha256(R.REFUTER_INSTRUCTION.encode("utf-8")).hexdigest()[:16]
    assert R._preamble_id() != old


def test_a_session_hands_its_procedure_to_the_prompt():
    s = R.RefuterSession(None, "https://x/", GOAL, ANSWER, procedure="drill: d")
    assert s.procedure == "drill: d"
    sent = []

    class Drv:
        socket = False

        def send(self, text):
            sent.append(text)

    s.drv = Drv()
    s.socket = False
    s._send_prompt()
    assert "drill: d" in sent[0] and R.SOURCE_CHECK_RULE in sent[0]
