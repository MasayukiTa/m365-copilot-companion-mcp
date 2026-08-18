"""Stage 0 の再生ハーネスそのものを検査する。

このファイルの中心は1つ: **truncation が実在すれば検出できること**。
再生ツールが構造的にゼロしか返せないなら、出力の 0/120 は「起きなかった」ではなく
「測れなかった」であり、その2つは見分けがつかない。実際この節を書く前に、私は
CLI 既定の dwell を渡して両アームが一度も受理しない状態で 0/120 を出しており、
それは「差が無い」と完全に同じ見た目をしていた。
"""
import json

import pytest

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "_stage0", ROOT / "scripts" / "settle_stage0_replay.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)


def _row(tid, phase, ts, text, **extra):
    row = {"turn_id": tid, "phase": phase, "ts": ts, "text": text,
           "generating": False, "text_len": len(text)}
    row.update(extra)
    return row


def _turn(*, pre, post, need_dwell=4.0, need_samples=6, marker=False):
    """1ターン分の行。pre は (ts, text) の列、post はラベル用の追い読み。"""
    rows = []
    last = None
    for ts, text in pre:
        phase = "stable" if text == last else "changed"
        extra = ({"need_dwell": need_dwell, "need_samples": need_samples, "marker": marker}
                 if phase == "stable" else {})
        rows.append(_row("p0|t1", phase, ts, text, **extra))
        last = text
    for ts, text in post:
        rows.append(_row("p0|t1", "post_accept", ts, text, post_accept=True))
    return rows


# ---- 中心: 実在する truncation を検出できるか ---------------------------------------------------

def test_a_real_truncation_is_detected_rather_than_reported_as_zero():
    """途中形で受理し、そのあと続きが届いたターン。両アームとも truncated と出るべき。

    ゼロしか返せないハーネスは、健全な標本と壊れたプローブを区別できない。"""
    rows = _turn(
        pre=[(0.0, "途中まで"), (1.0, "途中まで"), (2.0, "途中まで"),
             (3.0, "途中まで"), (4.0, "途中まで"), (5.0, "途中まで"), (6.0, "途中まで")],
        post=[(7.0, "途中まで、そして続きが届いた")])
    got = R.replay(rows, dwell_s=2.0, samples_needed=3)
    assert got is not None
    assert got["legacy_truncated"] is True
    assert got["unified_truncated"] is True, "続きが届いたのに truncation を数えていない"


def test_a_turn_that_settled_properly_is_not_counted_as_truncated():
    """逆方向。何でも truncated にする検出器も同じくらい役に立たない。"""
    rows = _turn(
        pre=[(0.0, "完成"), (1.0, "完成"), (2.0, "完成"), (3.0, "完成"),
             (4.0, "完成"), (5.0, "完成"), (6.0, "完成")],
        post=[(7.0, "完成"), (8.0, "完成")])
    got = R.replay(rows, dwell_s=2.0, samples_needed=3)
    assert got["legacy_truncated"] is False and got["unified_truncated"] is False


# ---- パラメータは推測しない -----------------------------------------------------------------------

def test_the_replay_uses_the_parameters_the_run_was_recorded_under():
    """記録時と違う設定で再生したら、それは別の実験。実際にこれを間違えて
    両アームが一度も受理せず、0/120 を「差が無い」と読みかけた。"""
    rows = _turn(
        pre=[(0.0, "本文"), (1.0, "本文"), (2.0, "本文"), (3.0, "本文"),
             (4.0, "本文"), (5.0, "本文"), (6.0, "本文")],
        post=[(7.0, "本文")],
        need_dwell=4.0, need_samples=6, marker=False)
    # 呼び出し側が渡す dwell は「意図的に大きすぎる」値。トレースの値が勝つべき。
    got = R.replay(rows, dwell_s=999.0, samples_needed=999)
    assert got["unified_index"] is not None, (
        "CLI 既定がトレースの記録値を上書きし、受理が起きなかった")
    assert got["legacy_index"] is not None


def test_a_turn_with_no_stable_row_falls_back_rather_than_crashing():
    rows = _turn(pre=[(0.0, "a"), (1.0, "b")], post=[(2.0, "b")])
    assert R.replay(rows, dwell_s=2.0, samples_needed=3) is not None


# ---- ラベルの出所 -----------------------------------------------------------------------------

def test_the_label_comes_from_after_the_accept_not_from_the_last_pre_sample():
    """最後の pre サンプルは「本番が受理したテキスト」。それをラベルにすると
    truncation は定義上ゼロになり、しかも本番が truncate したときに限ってゼロになる。"""
    rows = _turn(
        pre=[(0.0, "途中"), (1.0, "途中"), (2.0, "途中"), (3.0, "途中"),
             (4.0, "途中"), (5.0, "途中"), (6.0, "途中")],
        post=[(7.0, "途中と続き")])
    got = R.replay(rows, dwell_s=2.0, samples_needed=3)
    assert got["label_len"] == len("途中と続き")


def test_the_observation_window_is_reported_so_a_zero_can_be_qualified():
    """「見なかった」と「無かった」を出力から区別できること。"""
    rows = _turn(
        pre=[(0.0, "x"), (1.0, "x"), (2.0, "x"), (3.0, "x"), (4.0, "x"), (5.0, "x")],
        post=[(6.0, "x"), (9.5, "x")])
    got = R.replay(rows, dwell_s=2.0, samples_needed=3)
    assert got["tail_watched_s"] == 3.5


# ---- クラスタ ---------------------------------------------------------------------------------

def test_turns_sharing_a_prompt_are_counted_as_one_cluster(tmp_path):
    """12プロンプト×10周は120観測ではない。区間を120で計算すると狭すぎる。"""
    path = tmp_path / "t.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for prompt in range(3):
            for turn in range(4):
                tid = "p%02d|t%d" % (prompt, turn)
                for ts, text in ((0.0, "a"), (1.0, "a"), (2.0, "a")):
                    f.write(json.dumps(_row(tid, "stable", ts, text, need_dwell=4.0,
                                            need_samples=6, marker=False)) + "\n")
    turns = R.load_turns(path)
    assert len(turns) == 12
    assert len({t.split("|")[0] for t in turns}) == 3


def test_an_empty_or_missing_trace_is_refused_rather_than_reported_as_a_clean_run(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert R.load_turns(empty) == {}
