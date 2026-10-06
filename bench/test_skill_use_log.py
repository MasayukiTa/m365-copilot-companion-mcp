# -*- coding: utf-8 -*-
"""3週間書かれ続けて、読み手が1つも到達可能でなかった記録。

`tools/skill_ops.py` は3箇所から `.fleet/skill_use.jsonl` に書いている。2026-09-19 実測で
**789件、08-28 から 09-18**。このモジュールの読み手は全部死んでいた:

* `compare_runs` — 呼出元なし
* `observe` — `compare_runs` の中に `observe(s, e, path) if False else {...}` として
  **リテラルで切られた呼び出し**があり、その中身が else 側に**より狭い形で再実装**されていた

モジュール冒頭は「この実験は測ろうとしているものを観測する手段を持たなかった」と書いている。
**観測は存在していて、それを見る手段のほうが無かった。**

さらに、どちらの写しも `inject` を数えていなかった（実測 181件 / 789）。
**要約している記録の5分の1を黙って落とす読み手は、読み手が無いより悪い** — 合計が
合計の顔をしたままになる。
"""
from __future__ import annotations

import json

import pytest

from bench import skill_use_log as S


def _log(tmp_path, rows):
    p = tmp_path / "skill_use.jsonl"
    with open(str(p), "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return str(p)


def _match(ts, matched=""):
    return {"ts": ts, "kind": "match", "query": "q", "matched": matched}


# ─────────────────────────────────────────── 3つ目の種別

def test_injected_records_are_counted(tmp_path):
    """**実測181件がどの読み手からも見えていなかった。**"""
    p = _log(tmp_path, [_match(10), {"ts": 11, "kind": "inject"},
                        {"ts": 12, "kind": "load", "matched": "x"}])
    seen = S.observe(0, 100, p)
    assert (seen["matched"], seen["injected"], seen["loaded"]) == (1, 1, 1)


# ─────────────────────────────────────────── 写しが1つになったこと

def test_compare_runs_goes_through_observe(tmp_path):
    """`if False` で切られていた呼び出しが生きていること。ソースではなく振る舞いで見る:
    `observe` を差し替えて、`compare_runs` がそれを通るかを確かめる。"""
    p = _log(tmp_path, [_match(10, "a")])
    calls = []
    real = S.observe

    def _spy(start, end, path=S.DEFAULT_LOG, rows=None):
        calls.append((start, end, rows is not None))
        return real(start, end, path, rows)

    S.observe = _spy
    try:
        S.compare_runs({"arm": [(0, 100)]}, p)
    finally:
        S.observe = real
    assert calls, "compare_runs no longer calls observe"
    assert calls[0][2], "observe is being made to re-read the file per window"


def test_the_log_is_read_once_for_all_the_windows(tmp_path):
    """インラインの写しが存在した理由そのもの。窓ごとに読み直すなら、写しを消した意味が
    半分無くなる。"""
    p = _log(tmp_path, [_match(t) for t in range(10)])
    reads = []
    real = S.read

    def _spy(path=S.DEFAULT_LOG):
        reads.append(path)
        return real(path)

    S.read = _spy
    try:
        S.compare_runs({"arm": [(0, 1), (2, 3), (4, 5), (6, 7)]}, p)
    finally:
        S.read = real
    assert len(reads) == 1, "read %d times for 4 windows" % len(reads)


def test_the_rates_are_unchanged_by_the_rewrite(tmp_path):
    """狭い写しを消すときに、出力の意味まで変えていないこと。"""
    p = _log(tmp_path, [_match(1, "a"), {"ts": 2, "kind": "load", "matched": "a"},
                        _match(50)])
    out = S.compare_runs({"arm": [(0, 10), (40, 60)]}, p)["arm"]
    assert out["n"] == 2
    assert out["consulted_runs"] == 2       # どちらの窓にも match がある
    assert out["loaded_runs"] == 1
    assert out["consult_rate"] == 1.0
    assert out["load_rate"] == 0.5


def test_no_windows_is_not_a_division_by_zero(tmp_path):
    p = _log(tmp_path, [_match(1)])
    assert S.compare_runs({"arm": []}, p)["arm"]["n"] == 0


# ─────────────────────────────────────────── 読み手

def test_the_report_names_the_gap_rather_than_leaving_it_to_be_subtracted(tmp_path):
    """**実測: 566 match のうち何かに当たったのは107、459件はどのスキルにも当たっていない。**
    3つの種別は工程ではなく、間隔のほうが所見。件数だけ並べたら読み手が引き算させられる。"""
    p = _log(tmp_path, [_match(1, "alpha"), _match(2), _match(3)])
    out = S.report(p)
    assert "2 queries matched no skill at all" in out
    assert "alpha" in out


def test_an_empty_log_says_nothing_rather_than_zero(tmp_path):
    """「記録が無い」と「0件だった」を取り違えさせない。"""
    p = _log(tmp_path, [])
    assert "no records" in S.report(p)


def test_a_missing_file_reads_the_same_as_an_empty_one_and_says_so(tmp_path):
    """ログは最初の書き込みで初めて作られるので、この2つを区別する事実を持っていない。
    持っていない区別を主張しない。"""
    assert "no records" in S.report(str(tmp_path / "never_written.jsonl"))


def test_records_with_no_usable_timestamp_do_not_produce_a_fake_span(tmp_path):
    """時刻の無い行から期間を作ったら、測っていないものを報告することになる。"""
    p = _log(tmp_path, [{"kind": "match", "matched": "a"},
                        {"ts": "not a time", "kind": "match"}])
    out = S.report(p)
    assert "none with a usable timestamp" in out
    assert "->" not in out


def test_a_half_written_last_line_does_not_stop_the_report(tmp_path):
    """追記専用のログは、書いている最中に読まれる。"""
    p = tmp_path / "skill_use.jsonl"
    with open(str(p), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(_match(1, "alpha")) + "\n")
        fh.write('{"ts": 2, "kind": "ma')
    out = S.report(str(p))
    assert "1 records" in out and "alpha" in out


@pytest.mark.parametrize("bound", [(0, 5), (15, 20)])
def test_a_window_excludes_what_lies_outside_it(tmp_path, bound):
    """`within` の両端が必須である理由 — 隣の run の相談を自分のものとして数えない。"""
    p = _log(tmp_path, [_match(10, "a")])
    assert S.observe(bound[0], bound[1], p)["matched"] == 0
