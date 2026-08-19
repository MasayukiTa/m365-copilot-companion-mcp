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
from scripts import collect_lens_corpus as CL
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
    assert '"verdict": verdict if verdict in A.VERDICTS else A.UNCLEAR' in src


# ---- レンズが黙ったとき、それを意見として記録しないこと -------------------------------------------

def test_a_run_that_could_only_produce_unclear_is_refused_before_it_starts(monkeypatch):
    """レビュア用タブが開けない箱では、全レンズが UNCLEAR になる。
    それは『見て判断がつかなかった』と同じ値で記録され、全方策が同点になる。
    実際に一度、22分かけて全 UNCLEAR の1行だけを作った。"""
    monkeypatch.setattr("relay.relay_fleet.ram_room_for_tab", lambda: False)
    monkeypatch.setattr("relay.relay_fleet.avail_phys_mb", lambda: 954.0)
    try:
        CL.require_room_for_lenses()
    except CL.NotEnoughRoom as exc:
        assert "954" in str(exc) and "do not lower the floor" in str(exc)
    else:
        raise AssertionError("床を満たさない箱で収集を始めてはいけない")


def test_room_present_means_no_refusal(monkeypatch):
    monkeypatch.setattr("relay.relay_fleet.ram_room_for_tab", lambda: True)
    CL.require_room_for_lenses()


def test_the_reason_is_kept_so_a_silent_lens_can_be_told_from_an_unsure_one():
    src = Path(CL.__file__).read_text(encoding="utf-8")
    assert '"reason": reason or ""' in src
    assert "verdict, _reason = got" not in src, "reason を捨てると診断が消える"


def test_an_all_silent_panel_becomes_a_skip_not_a_row():
    detail = {"correctness": {"verdict": "UNCLEAR", "reason": ""},
              "edge": {"verdict": "UNCLEAR", "reason": ""},
              "security": {"verdict": "UNCLEAR", "reason": ""}}
    assert CL.all_unclear(detail)
    detail["edge"]["verdict"] = "UPHELD"
    assert not CL.all_unclear(detail)
    assert CL.verdicts_only(detail)["edge"] == "UPHELD"


# ---- 穴と、答えとしての UNCLEAR は別物 ------------------------------------------------------------

def test_a_candidate_every_lens_found_unclear_is_kept():
    """3本とも聞かれて誰も評決を出せなかったなら、それは候補の性質。
    どの方策も同じ点になるのが正しく、捨てると本物の観測を失う。"""
    detail = {ln: {"verdict": "UNCLEAR", "elapsed_s": 200.0,
                   "reason": "the nudge budget ran out without a parseable verdict"}
              for ln in ("correctness", "edge", "security")}
    assert CL.all_unclear(detail)
    assert CL.harness_faults(detail) == [], "レビュアの答えを障害として扱っている"
    assert CL.timed_out_lenses(detail) == []


def test_a_lens_that_could_not_be_asked_is_a_hole():
    """実測された組み合わせ: correctness はページが開けず、edge と security は
    答えたうえで評決を出せなかった。行ごと捨てると、1本の障害のために
    2本の本物の観測を失う -- ので行は落とすが、理由は『聞けなかった方』を指す。"""
    detail = {
        "correctness": {"verdict": "UNCLEAR", "elapsed_s": 12.0,
                        "reason": "harness: opening the side page failed: RuntimeError: x"},
        "edge": {"verdict": "UNCLEAR", "elapsed_s": 210.0,
                 "reason": "the nudge budget ran out without a parseable verdict"},
        "security": {"verdict": "UNCLEAR", "elapsed_s": 205.0,
                     "reason": "the nudge budget ran out without a parseable verdict"},
    }
    assert CL.harness_faults(detail) == ["correctness"]


def test_the_prefix_is_what_separates_them_not_a_string_table():
    from relay.refuter import HARNESS_REASON_PREFIX, unclear_is_harness_fault
    assert unclear_is_harness_fault(HARNESS_REASON_PREFIX + "anything")
    assert not unclear_is_harness_fault("anything")
    # 新しい出口が分類を忘れたら、レビュアの答え側に落ちる -- そちらが安全側
    assert not unclear_is_harness_fault(None)


def test_every_session_exit_that_cannot_ask_is_marked():
    """出口を足した人が分類を忘れると、穴が観測として記録される。"""
    from pathlib import Path
    import relay.refuter as R
    src = Path(R.__file__).read_text(encoding="utf-8")
    needle = '_finish(("UNCLEAR", '
    exits = []
    at = src.find(needle)
    while at != -1:
        line = src[at:src.index(chr(10), at)]
        exits.append(line[len(needle):].rstrip().rstrip(")").strip())
        at = src.find(needle, at + 1)
    assert len(exits) >= 8, exits
    unmarked = [e for e in exits if "HARNESS_REASON_PREFIX" not in e]
    assert unmarked == ['"the nudge budget ran out without a parseable verdict"'], unmarked


def test_only_a_lens_that_could_not_be_asked_is_retried():
    """評決が得られなかった場合のみ張り直す。気に入らない評決を引き直すのとは別物で、
    前者は穴を観測に変えるだけ、後者は観測そのものを動かす。"""
    src = Path(CL.__file__).read_text(encoding="utf-8")
    i = src.index("for attempt in range(1 + LENS_RETRIES):")
    body = src[i:src.index("def verdicts_only", i)]
    assert "if not unclear_is_harness_fault(out[lens][" in body, (
        "レビュアが答えた場合でも張り直している")
    assert "break" in body


def test_the_retry_count_is_recorded():
    """3回目でようやく取れた評決と、一発で取れた評決は、同じではない。"""
    src = Path(CL.__file__).read_text(encoding="utf-8")
    assert '"attempts": attempt + 1' in src
