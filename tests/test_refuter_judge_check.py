# -*- coding: utf-8 -*-
"""レビュアー(refuter)は、データ解析系の回答に対し『最終判定器を通したか、通していなければ
なぜか』を必ず確認しなければならない。

背景(オーナー指示 2026-10-27 / 2026-10-30): データ解析系の仕事は最終で判定器を通す。
その履行を refuter が毎回点検するよう、判定器チェックを全プロンプトに載せる。

このチェック文は完全にドメインフリー(特定システム・ツール・データの名前を含まない)でなければ
ならない -- tests/test_no_domain_words_in_harness.py の規律に従う。
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import refuter as R  # noqa: E402

GOAL = "この群間比較で差が有意か判定して"
ANSWER = "差は 2.3。出典: 登録済みの集計。"

LENSES = ["", "correctness", "edge", "security", "minimality", "answer_minimality"]
FRAMES = [False, True]


@pytest.mark.parametrize("lens", LENSES)
@pytest.mark.parametrize("unverifiable", FRAMES)
def test_every_prompt_carries_the_judge_check(lens, unverifiable):
    got = R.build_refuter_prompt(GOAL, ANSWER, lens=lens, unverifiable=unverifiable)
    assert R.JUDGE_CHECK_RULE in got


def test_judge_check_rule_demands_pass_or_reason():
    rule = R.JUDGE_CHECK_RULE
    # 判定器を通したか、通していなければ理由、の両輪が文面にある
    assert "判定器" in rule
    assert "REFUTED" in rule
    # 理由(通していない場合)を要求している
    assert "理由" in rule or "なぜ" in rule


def test_judge_check_rule_is_domain_free():
    # ドメイン語(技術語の最低限セット)を含まない
    import re
    pat = re.compile(r"odbc|sql\s*server", re.IGNORECASE)
    assert pat.search(R.JUDGE_CHECK_RULE) is None
