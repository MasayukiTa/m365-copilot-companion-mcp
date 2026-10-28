"""Structured conclusions distinguish changes hidden by similar Japanese prose."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from relay import relay_fleet as fleet

BOILERPLATE = "STUCK: 対象の確認を実施しましたが処理を続行できません。調査結果と確認内容を記録しました。"
A = BOILERPLATE + "\nCONCLUSION: kind=access; evidence=error_a; next=review"
B = BOILERPLATE + "\nCONCLUSION: kind=network; evidence=error_b; next=retry"


def judge(a, b, previous_resp=None, current_resp=None):
    # On the old baseline, exercise the actual historical decision to demonstrate RED.
    v2 = getattr(fleet, "_stuck_converged_v2", None)
    return v2(a, b, previous_resp, current_resp) if v2 else fleet._stuck_converged(a, b)


def test_similar_words_but_different_conclusions_do_not_converge():
    words_a, words_b = fleet._stuck_words(A), fleet._stuck_words(B)
    assert len(words_a & words_b) / len(words_a | words_b) >= 0.7
    assert fleet._stuck_converged(A, B) is True
    assert judge(A, B) is False


def test_identical_conclusions_converge():
    assert judge(A, A) is True


@pytest.mark.parametrize("changed", [
    "CONCLUSION: kind=network; evidence=error_a; next=review",
    "CONCLUSION: kind=access; evidence=error_b; next=review",
    "CONCLUSION: kind=access; evidence=error_a; next=retry",
])
def test_each_conclusion_field_matters(changed):
    assert judge(A, BOILERPLATE + "\n" + changed) is False


def test_completion_reply_never_converges():
    for ending in ("DONE", "完了", "完了しました"):
        assert judge(A, A, A, A + "\n" + ending) is False
        assert judge(A, A, A + "\n" + ending, A) is False
