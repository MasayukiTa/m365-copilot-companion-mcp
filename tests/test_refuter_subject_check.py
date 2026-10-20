# -*- coding: utf-8 -*-
"""The reviewer must check that the answer came from the thing the QUESTION names.

MEASURED 2026-10-18 (two routes, same class of error). A count question was answered from a
look-alike source (3,818 where the right source gives 13,218); the reviewer recomputed the number
from that same look-alike, found it consistent, and UPHELD. SOURCE_CHECK_RULE only compares the
answer against a matched approved procedure; with no procedure there was nothing that asked
"is this the object the question is about?".

The rule is generic: it names no system, tool or topic. tests/test_no_domain_words_in_harness.py
keeps it that way.
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import refuter as R  # noqa: E402

GOAL = "8月の不良件数を教えて"
ANSWER = "8月の不良件数は 3,818 件でした。"

LENSES = ["", "correctness", "edge", "security", "minimality", "rootcause", "rootcause_code"]


@pytest.mark.parametrize("lens", LENSES)
@pytest.mark.parametrize("unverifiable", [False, True])
def test_every_prompt_carries_the_subject_check(lens, unverifiable):
    got = R.build_refuter_prompt(GOAL, ANSWER, lens=lens, unverifiable=unverifiable)
    assert R.SUBJECT_CHECK_RULE in got
    # the reviewer reads the rule before the goal and the report
    assert got.index(R.SUBJECT_CHECK_RULE) < got.index("--- ゴール ---")


def test_the_rule_asks_for_the_named_object_and_a_refuting_verdict_format():
    r = R.SUBJECT_CHECK_RULE
    assert "名指し" in r and "対象違い" in r and "REFUTED" in r


def test_a_same_quantity_in_another_object_is_called_out():
    r = R.SUBJECT_CHECK_RULE
    assert "似た" in r and "同じ量" in r


def test_an_answer_that_hides_its_object_is_not_upheld_by_default():
    assert "INCONCLUSIVE" in R.SUBJECT_CHECK_RULE


def test_the_source_check_is_still_there_and_comes_first():
    got = R.build_refuter_prompt(GOAL, ANSWER)
    assert got.index(R.SOURCE_CHECK_RULE) < got.index(R.SUBJECT_CHECK_RULE)


def test_the_preamble_id_changes_with_the_rule(monkeypatch):
    before = R._preamble_id()
    monkeypatch.setattr(R, "SUBJECT_CHECK_RULE", R.SUBJECT_CHECK_RULE + "x")
    assert R._preamble_id() != before
