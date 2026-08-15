"""Stage 0's replay: does it measure what the plan says, and does it refuse when it cannot.

The refusals matter as much as the measurement. A trace recorded in the ordinary debugging
mode can be summarised into a truncation number, and that number would describe a population
of slow turns rather than the early accepts the endpoint is about -- a confident answer to a
different question, which is the failure this package exists to avoid.
"""
from __future__ import annotations

import io
import json
import os
import tempfile

import pytest

from relay import settle_replay as SR


def _write(rows):
    path = os.path.join(tempfile.mkdtemp(prefix="trace_"), "settle_trace.jsonl")
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def _poll(turn, text, generating=False, post_accept=False):
    row = {"turn_id": turn, "text": text, "generating": generating,
           "text_len": len(text), "phase": "stable"}
    if post_accept:
        row["post_accept"] = True
        row["phase"] = "post_accept"
    return row


def _turn(turn, texts, tail=()):
    """A recorded turn: what the arms may decide on, then the label-only tail.

    Every constructed turn gets a tail, because a turn WITHOUT one has production's accepted
    text as its label and can only ever score zero -- which is the defect these tests exist
    to keep out.
    """
    rows = [_poll(turn, t) for t in texts]
    rows += [_poll(turn, t, post_accept=True) for t in (tail or [texts[-1]])]
    return rows


# ---- the refusals -----------------------------------------------------------------------

def test_a_debug_mode_trace_is_refused_rather_than_summarised():
    """turn_id も全文も無いトレースからでも数字は出せる。その数字は
    別の母集団についての答えなので、出さないほうが正しい。"""
    path = _write([{"ts": 1, "age_s": 61.0, "phase": "stable", "text_len": 58,
                    "text_tail": "...", "generating": False, "stable_count": 3}])
    with pytest.raises(SR.NotReplayable) as exc:
        SR.load_turns(path)
    assert "collect-mode" in str(exc.value)
    assert "60-second gate" in str(exc.value)


def test_an_empty_trace_is_refused_and_the_refusal_says_why_that_is_interesting():
    path = _write([])
    with pytest.raises(SR.NotReplayable) as exc:
        SR.load_turns(path)
    assert "empty trace is a real finding" in str(exc.value)


def test_a_missing_trace_is_refused():
    with pytest.raises(SR.NotReplayable):
        SR.load_turns(os.path.join(tempfile.mkdtemp(), "absent.jsonl"))


# ---- the two implementations ---------------------------------------------------------------

def test_the_legacy_predicate_accepts_on_two_identical_reads():
    polls = [_poll("t1", "partial"), _poll("t1", "partial"), _poll("t1", "partial and more")]
    assert SR.accept_index_legacy(polls) == 1


def test_the_sampled_predicate_waits_for_a_sample_floor():
    """同じ内容の2回読みは、まだ終わっていないストリームの一時停止の中に
    両方とも収まりうる。それが truncated capture の作られ方。"""
    polls = [_poll("t1", "partial"), _poll("t1", "partial"), _poll("t1", "partial and more"),
             _poll("t1", "partial and more"), _poll("t1", "partial and more")]
    assert SR.accept_index_legacy(polls) == 1        # accepts the pause
    # THREE CONSECUTIVE READS OF THIS TEXT, not three informative samples ever seen. The floor
    # used to count a running total that was never reset, so after any earlier partial read
    # the first two identical reads in a later pause already satisfied it -- the arm collapsed
    # to the legacy predicate exactly where the floor was supposed to bite.
    assert SR.accept_index_sampled(polls) == 4       # waits, and gets the whole thing


def test_the_floor_is_not_satisfied_by_samples_of_a_different_text():
    """『答えを3回見た』が目的の規則を、別の文字列を見た回数で満たしてはいけない。"""
    polls = [{"text": "a"}, {"text": "b"}, {"text": "c"}, {"text": "c"}]
    assert SR.accept_index_legacy(polls) == 3
    assert SR.accept_index_sampled(polls) == -1


def test_generation_resets_stability_in_both():
    polls = [_poll("t1", "a"), _poll("t1", "a", generating=True), _poll("t1", "a")]
    assert SR.accept_index_legacy(polls) == -1
    assert SR.accept_index_sampled(polls) == -1


def test_a_turn_that_never_stabilises_is_counted_as_never_rather_than_truncated():
    polls = [_poll("t1", "a"), _poll("t1", "b"), _poll("t1", "c")]
    assert SR.accept_index_legacy(polls) == -1


# ---- the comparison --------------------------------------------------------------------------

def test_truncation_is_measured_against_the_turns_final_text():
    """計画が定める正解ラベルそのもの。受理点が末尾と違えば truncated。"""
    turns = SR.load_turns(_write(_turn(
        "t1", ["half", "half", "half and the rest", "half and the rest",
               "half and the rest"], tail=["half and the rest"])))
    out = SR.replay(turns)
    assert out["per_implementation"]["legacy"]["truncated"] == 1
    assert out["per_implementation"]["sampled"]["truncated"] == 0


def test_the_discordant_pairs_are_reported_because_the_pairing_is_the_point():
    """2つの率だけ出すと対応が消える。"""
    rows = []
    for i in range(3):
        rows += _turn("t%d" % i, ["half", "half", "half and rest", "half and rest",
                                  "half and rest"])
    out = SR.replay(SR.load_turns(_write(rows)))
    assert out["discordant"]["legacy_worse"] == 3
    assert out["discordant"]["sampled_worse"] == 0


# ---- the label ---------------------------------------------------------------------------

def test_the_label_comes_from_after_production_accepted():
    """`_settle_trace` は settle ループの中から呼ばれ、ループは本番が受理した時点で終わる。
    受理後を記録しなければ、最後のテキストは『本番が受理したテキスト』であり、
    本番より弱い両腕はそれ以前でしか受理しない -- truncation は定義上ゼロになる。
    狙っていた truncation は、本番が切った瞬間に記録が止まるので、まさにその時ゼロと数えられる。"""
    # production accepted "half"; the text actually went on to grow.
    rows = _turn("t1", ["half", "half"], tail=["half and the rest"])
    out = SR.replay(SR.load_turns(_write(rows)))
    assert out["per_implementation"]["legacy"]["truncated"] == 1,         "受理後の記録が正解ラベルとして使われていない"


def test_without_a_tail_the_turn_is_reported_as_unlabelled_rather_than_as_clean():
    """ラベルの無いターンから出るゼロは、測定ではなく定義。"""
    rows = [_poll("t1", "half"), _poll("t1", "half")]
    out = SR.replay(SR.load_turns(_write(rows)))
    assert out["unlabelled_turns"] == 1
    assert "zero by construction" in SR.report(out)


def test_the_post_accept_samples_are_never_offered_as_decision_points():
    """本番が既に戻った後のサンプルで受理する述語は、誰も走らせられない述語。"""
    rows = _turn("t1", ["a", "a"], tail=["a", "a", "a"])
    out = SR.replay(SR.load_turns(_write(rows)))
    assert out["rows"][0]["legacy"]["index"] < 2
    assert out["rows"][0]["polls"] == 2


# ---- what may and may not be claimed ------------------------------------------------------

def test_there_is_no_significance_test_because_there_is_no_null_that_could_be_true():
    """sampled の条件は legacy の条件の真部分集合(floor が増えるだけ)なので、
    sampled が先に受理することはありえない。McNemar の帰無仮説 p=0.5 は成立しない。
    p値は『起こりえないことが起きなかった』としか言わない。"""
    assert not hasattr(SR, "mcnemar")
    rows = _turn("t1", ["half", "half", "whole", "whole", "whole"])
    out = SR.replay(SR.load_turns(_write(rows)))
    assert "sufficiently_powered" not in out
    assert "p_value" not in out


def test_sampled_can_never_accept_earlier_than_legacy():
    """構造的優越を実挙動で固定する。ここが崩れたら報告の形も変わる。"""
    import random
    rng = random.Random(11)
    for _ in range(200):
        polls = []
        text = ""
        for _ in range(rng.randint(2, 12)):
            if rng.random() < 0.4:
                text += "x" * rng.randint(1, 4)
            polls.append({"text": text, "generating": rng.random() < 0.15})
        legacy = SR.accept_index_legacy(polls)
        sampled = SR.accept_index_sampled(polls)
        if legacy >= 0 and sampled >= 0:
            assert sampled >= legacy


def test_the_reduction_is_reported_as_a_size_with_an_interval():
    # legacy accepts the pause at "half"; sampled waits for three reads of "half and rest".
    rows = []
    for i in range(4):
        rows += _turn("t%d" % i, ["half", "half", "half and rest", "half and rest",
                                  "half and rest"], tail=["half and rest"])
    out = SR.replay(SR.load_turns(_write(rows)))
    r = out["reduction"]
    assert r["turns_fixed"] == 4 and r["turns"] == 4
    assert r["ci95"][0] <= r["point"] <= r["ci95"][1]
    assert "no null that could be true" in r["note"]


def test_the_interval_behaves_at_the_boundaries():
    """0件と全件で正規近似は壊れる。Wilson は壊れない。"""
    low, high = SR._wilson(0, 20)
    assert low == 0.0 and 0 < high < 1
    low, high = SR._wilson(20, 20)
    assert 0 < low < 1 and high == 1.0


# ---- the cost, which was dropped from the comparison ---------------------------------------

def test_a_turn_one_arm_never_accepted_is_not_dropped_from_the_comparison():
    """『受理できなかった』は sampled 唯一の失敗様式(floor が本番の timeout まで持ちこたえる)。
    片方の腕の失敗だけが集計から落ちる設計になっていた。"""
    # legacy accepts at index 1; sampled's floor of 3 is never reached.
    rows = [_poll("t1", "a"), _poll("t1", "a"), _poll("t1", "a", post_accept=True)]
    out = SR.replay(SR.load_turns(_write(rows)))
    assert out["per_implementation"]["sampled"]["never"] == 1
    assert out["never_only"]["sampled"] == 1
    assert "timeout in production" in SR.report(out)


# ---- grouping ------------------------------------------------------------------------------

def test_a_trace_whose_format_changed_part_way_is_refused():
    """rows[0] だけ見る検査は、途中で形式が変わったトレースを素通りさせる。"""
    rows = _turn("t1", ["a", "a"])
    rows.append({"ts": 1, "phase": "stable", "text_len": 3})
    with pytest.raises(SR.NotReplayable) as exc:
        SR.load_turns(_write(rows))
    assert "not fully collect-mode" in str(exc.value)


def test_unlabelled_rows_are_refused_rather_than_merged():
    """空の turn_id をまとめると、無関係なポーリングから1つの巨大な擬似ターンができ、
    その正解ラベルはたまたま最後だったターンのものになる。"""
    rows = _turn("t1", ["a", "a"])
    rows.append({"turn_id": "", "text": "b", "generating": False})
    with pytest.raises(SR.NotReplayable) as exc:
        SR.load_turns(_write(rows))
    assert "empty turn_id" in str(exc.value)


# ---- fidelity the two arms SHARE ----------------------------------------------------------

def test_a_processing_placeholder_is_not_an_answer_in_either_arm():
    """スロットリングのターンが `情報を整理しています…` を受理し、両腕で truncated と数えられた。
    本番はプレースホルダを飛ばす。これは腕の差ではなく共通の忠実度で、
    実行3件しかない truncation のうち1件がこの人工物だった。"""
    rows = _turn("t1", ["情報を整理しています…", "情報を整理しています…", "real answer",
                        "real answer"], tail=["real answer"])
    out = SR.replay(SR.load_turns(_write(rows)))
    assert out["per_implementation"]["legacy"]["truncated"] == 0


def test_the_sample_floor_counts_informative_samples_not_placeholders():
    """『答えを3回見た』が目的の規則を、処理中表示の連打で満たせてはいけない。"""
    polls = [{"text": "処理中です。"}, {"text": "処理中です。"}, {"text": "処理中です。"},
             {"text": "answer"}, {"text": "answer"}]
    assert SR.accept_index_sampled(polls) == -1


# ---- clustering: 120 rows from 12 prompts is 12 units, not 120 ------------------------------

def _clustered(prompt, n, texts, tail):
    rows = []
    for i in range(n):
        rows += _turn("%s|t%d" % (prompt, i), texts, tail=tail)
    return rows


def test_the_interval_is_computed_over_clusters_when_the_turns_are_labelled():
    """12プロンプト×10巡は120の観測ではなく12の独立単位。
    120として区間を出すと、相関した反復を新しい証拠として数え、幅が不当に狭くなる。"""
    rows = _clustered("p00", 10, ["half", "half"], ["half and rest"])
    rows += _clustered("p01", 10, ["whole", "whole"], ["whole"])
    out = SR.replay(SR.load_turns(_write(rows)))
    r = out["reduction"]
    assert "cluster bootstrap" in r["method"]
    assert "2 prompt" in r["method"]


def test_the_clustered_interval_is_wider_than_the_turn_level_one():
    """狭い区間は、証拠が多いように見せる。ここが今回いちばん誤解を生む場所。"""
    rows = _clustered("p00", 10, ["half", "half"], ["half and rest"])
    rows += _clustered("p01", 10, ["whole", "whole"], ["whole"])
    out = SR.replay(SR.load_turns(_write(rows)))
    lo, hi = out["reduction"]["ci95"]
    wlo, whi = SR._wilson(out["reduction"]["turns_fixed"], out["reduction"]["turns"])
    assert (hi - lo) > (whi - wlo), "クラスタ補正が幅を広げていない"


def test_unlabelled_turns_fall_back_and_say_the_interval_is_not_corrected():
    """補正できないことを黙っているのが最悪。"""
    rows = _turn("t1", ["half", "half"], tail=["half and rest"])
    out = SR.replay(SR.load_turns(_write(rows)))
    assert "NOT cluster-corrected" in out["reduction"]["method"]


def test_the_bootstrap_does_not_move_between_identical_runs():
    """再実行のたびに動く区間は、望む答えが出るまで再実行させる。"""
    rows = _clustered("p00", 6, ["half", "half"], ["half and rest"])
    rows += _clustered("p01", 6, ["whole", "whole"], ["whole"])
    path = _write(rows)
    a = SR.replay(SR.load_turns(path))["reduction"]["ci95"]
    b = SR.replay(SR.load_turns(path))["reduction"]["ci95"]
    assert a == b


# ---- what the headline number may not be credited for ---------------------------------------

def test_a_predicate_that_never_accepts_scores_no_reduction():
    """受理しない述語は truncate もしない。totalの引き算だと『完璧に改善した』になる --
    実際に起きているのは本番の timeout。"""
    # legacy accepts at index 1; sampled's floor of three is never reached.
    rows = []
    for i in range(3):
        rows += [_poll("p0|t%d" % i, "half"), _poll("p0|t%d" % i, "half"),
                 _poll("p0|t%d" % i, "half and rest", post_accept=True)]
    out = SR.replay(SR.load_turns(_write(rows)))
    assert out["per_implementation"]["sampled"]["never"] == 3
    assert out["reduction"]["turns_fixed"] == 0, "受理しないことを改善として数えている"
    assert out["never_only"]["sampled"] == 3


def test_an_all_zero_bootstrap_does_not_claim_certainty():
    """観測ゼロの経験分布からは、未観測事象の確率を作れない。
    [0,0] は『ゼロだと確信している』ではなく『ブートストラップには言えない』。"""
    rows = []
    for p in range(12):
        rows += _turn("p%02d|t0" % p, ["whole", "whole", "whole"], tail=["whole"])
    out = SR.replay(SR.load_turns(_write(rows)))
    lo, hi = out["reduction"]["ci95"]
    assert lo == 0.0
    assert hi > 0.2, "全ゼロで区間が潰れている (%r)" % ((lo, hi),)


def test_an_unlabelled_turn_is_left_out_of_the_counts_and_the_denominator():
    """『scored ではなく unlabelled として報告する』と書きながら、
    実際には全ての集計と分母に入れていた。本番の受理点をラベルにしたターンが
    率をゼロ方向に薄める一方、警告行は逆のことを言っていた。"""
    rows = _turn("p0|t0", ["half", "half", "whole", "whole", "whole"], tail=["whole"])
    rows += [_poll("p0|t1", "half"), _poll("p0|t1", "half")]      # no tail
    out = SR.replay(SR.load_turns(_write(rows)))
    assert out["unlabelled_turns"] == 1
    assert out["turns"] == 1, "ラベルの無いターンが分母に残っている"
    assert out["per_implementation"]["legacy"]["accepted"] == 1


def test_a_tail_that_was_still_moving_does_not_establish_a_label():
    """見るのをやめたことを、変化が止まったことと取り違えると、
    本番の受理点をラベルにしたのと同じ置き換えになる。"""
    rows = _turn("p0|t0", ["half", "half"], tail=["half and", "half and the rest"])
    out = SR.replay(SR.load_turns(_write(rows)))
    assert out["unlabelled_turns"] == 1
    assert out["turns"] == 0


def test_a_trace_with_many_unparseable_lines_is_refused():
    """末尾の1行が途中なのは書き込み中で普通。それ以上は poll が失われていて、
    生き残った分だけで再生すると、誰にも突き合わせられない分母が出る。"""
    import io as _io
    import os as _os
    import tempfile
    path = _os.path.join(tempfile.mkdtemp(prefix="tr_"), "t.jsonl")
    with _io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in _turn("p0|t0", ["a", "a"], tail=["a"]):
            fh.write(json.dumps(r) + "\n")
        fh.write("{broken\n{also broken\n")
    with pytest.raises(SR.NotReplayable) as exc:
        SR.load_turns(path)
    assert "could not be parsed" in str(exc.value)


def test_one_trailing_partial_line_is_tolerated():
    import io as _io
    import os as _os
    import tempfile
    path = _os.path.join(tempfile.mkdtemp(prefix="tr_"), "t.jsonl")
    with _io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in _turn("p0|t0", ["a", "a"], tail=["a"]):
            fh.write(json.dumps(r) + "\n")
        fh.write('{"turn_id": "p0|t1", "te')
    assert len(SR.load_turns(path)) == 1
