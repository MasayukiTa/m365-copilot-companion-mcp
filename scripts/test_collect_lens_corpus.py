"""グレーダーの主張を、崩さずに §18 の行へ移せているか。

このコレクタで一番静かに壊れる場所は「地の真値」の作り方であって、収集ループではない。
返信が取れなければ 0 行になるだけで、それはすぐ気づく。だが真値の軸を取り違えた
コーパスは、行数も見た目も正常なまま、frontier に確信のある誤答を出す。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.companionbench.episode import GradeResult, COVERAGE_COMPLETE, COVERAGE_PARTIAL
from relay.selfimprove import reviewer_allocation as A
from scripts.collect_lens_corpus import truth_from_grade


def test_a_clean_grade_is_not_bad_on_either_axis():
    t = truth_from_grade(GradeResult(functional_score=1.0, security_score=1.0,
                                     side_effect_score=1.0,
                                     security_coverage=COVERAGE_COMPLETE))
    assert t == {"functional": True, "security": A.SECURITY_PASS}


def test_a_security_violation_does_not_also_mark_the_functional_axis():
    """`grade.success` を使うとここが False になる。3スコアの論理積だから。

    畳み込むと、セキュリティ違反が機能軸にも入る。すると『セキュリティ失敗だけを
    見逃す方策』が『機能失敗も見逃している』ように見え、per-class の分割が
    そもそも見ようとしていたものを消す。
    """
    g = GradeResult(functional_score=1.0, security_score=0.0, side_effect_score=1.0)
    assert g.success is False, "前提: success は論理積である"
    t = truth_from_grade(g)
    assert t["security"] == A.SECURITY_VIOLATION
    assert t["functional"] is True, "セキュリティ違反が機能軸に漏れている"


def test_partial_coverage_stays_unevaluable():
    t = truth_from_grade(GradeResult(functional_score=1.0, security_score=1.0,
                                     side_effect_score=1.0,
                                     security_coverage=COVERAGE_PARTIAL))
    assert t["security"] == A.SECURITY_UNEVALUABLE


def test_a_failing_score_outranks_a_clean_coverage_label():
    """2つは独立に設定できる。ラベルだけ読むと、見えた違反を pass として記録する。"""
    t = truth_from_grade(GradeResult(functional_score=1.0, security_score=0.5,
                                     side_effect_score=1.0,
                                     security_coverage=COVERAGE_COMPLETE))
    assert t["security"] == A.SECURITY_VIOLATION


def test_side_effects_land_on_the_functional_axis():
    t = truth_from_grade(GradeResult(functional_score=1.0, security_score=1.0,
                                     side_effect_score=0.0,
                                     security_coverage=COVERAGE_COMPLETE))
    assert t["functional"] is False


def test_the_truth_is_accepted_by_the_thing_that_will_consume_it():
    """行の形はここで決まるが、読むのは simulate。片方だけ直すと静かにずれる。"""
    row = {"candidate_id": "x",
           "bad": truth_from_grade(GradeResult(functional_score=0.0)),
           "verdicts": {lens: A.UPHELD for lens in ("correctness", "edge", "security")}}
    functional, security = A._truth(row)
    assert functional is False and security in (A.SECURITY_PASS, A.SECURITY_VIOLATION,
                                               A.SECURITY_UNEVALUABLE)


# ---- 空コーパスが「きれいな結果」に見えないこと ---------------------------------------------------

def test_the_collector_does_not_build_on_the_runner_row():
    """`run_episode` は返信を返さない。それに載せたコレクタは全件スキップし、
    出力は 0 行 -- 健全な走行と同じ見た目になる。実際に一度そう書いた。"""
    src = Path(__file__).resolve().parent.joinpath("collect_lens_corpus.py").read_text(
        encoding="utf-8")
    assert "from bench.companionbench.runner import run_episode" not in src
    assert "episode.setup(workdir)" in src


def test_a_lens_that_never_answered_is_recorded_as_unclear_not_upheld():
    src = Path(__file__).resolve().parent.joinpath("collect_lens_corpus.py").read_text(
        encoding="utf-8")
    assert "out[lens] = verdict if verdict in A.VERDICTS else A.UNCLEAR" in src
