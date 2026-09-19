# -*- coding: utf-8 -*-
"""発端を日付で特定するために作られ、特定する手段が無かったログ。

`bridge/copilot_bridge.py` は60秒ごとにブラウザのページ数を記録している。その上のコメントが
理由を書いている — 2026-09-10 に headless Edge が **71ページ**（うち69が1つのURLの孤児）を
抱えていて、「何時間も誰も気づかず、**あとから発端を日付で特定することすらできなかった**。
ページ数を時系列で記録したログがこの機械に一度も無かったから。プロセス数は何度も取られて
いたが構造的に役に立たない: 同一オリジンなので Chromium が70ページで約7レンダラを共有し、
プロセス数は17のまま平坦だった。**ページ数だけがこの種類を見られ、持続化されたものだけが
日付を特定できる**」。

以来ずっと書かれている。**一度も読まれていない** — 開くのは自分の刈り取り処理だけで、
それも短く書き直すために読んでいる。日付を特定するために在るログで、日付を特定できない。

## この計器は騒がしい場合を一度も見ていない

ログの開始は **2026-09-11 07:52** — 障害の**翌日**。実測で 12,255 サンプル / 210.2時間、
ピークは **4ページ**（閾値8）。**静かな場合しか観測していない計器は、騒がしい場合を
捕まえられると示されたことにならない。** だからここで 71ページの障害を合成して通す。

## 「起きていない」を主張するには被覆率が要る

サンプラが1日止まっていたログと、ブラウザが1日健全だったログは同じ形をしている。
そして問題の障害は**数時間**続いた。だから報告は必ず被覆率を併記する。
実測: 97%、未観測 3.6時間（最大の欠測は 09-11 の 3.4時間）。
"""
from __future__ import annotations

import json

from tools import page_count_report as R


def _log(tmp_path, rows):
    p = tmp_path / "page_counts.jsonl"
    with open(str(p), "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return str(p)


def _s(ts, pages, agent=0):
    return {"ts": float(ts), "cdp": "http://127.0.0.1:9223", "pages": pages, "agent": agent}


def _quiet(n, start=0.0, pages=2):
    return [_s(start + i * 60, pages) for i in range(n)]


# ───────────────────────────── 計器が一度も見ていない場合

def test_the_2026_09_10_incident_shape_is_caught_and_dated(tmp_path):
    """**合成した本番障害。** 71ページが数時間続く。読み手はそれを1件の発端として、
    開始時刻・ピーク・継続で返さなければならない — 当日に誰も出せなかった数字。"""
    rows = _quiet(30)                                   # 静かな30分
    onset_start = 30 * 60.0
    rows += [_s(onset_start + i * 60, 40 + i) for i in range(180)]   # 3時間かけて40→219
    rows += _quiet(30, start=onset_start + 180 * 60 + 60)            # 収束
    got = R.onsets(R.read(_log(tmp_path, rows)))
    assert len(got) == 1, "the leak was split into %d findings" % len(got)
    assert got[0]["started"] == onset_start
    assert got[0]["peak"] == 219
    assert got[0]["samples"] == 180
    out = R.report(_log(tmp_path, rows))
    assert "ONSET" in out and "peak 219" in out


def test_a_single_spike_is_still_reported(tmp_path):
    """1サンプルだけ閾値を超えるのは、開いている途中のタブかもしれない。
    黙って捨てると、発端の1分目を捨てることになる。"""
    rows = _quiet(10) + [_s(600, 20)] + _quiet(10, start=660)
    got = R.onsets(R.read(_log(tmp_path, rows)))
    assert len(got) == 1 and got[0]["samples"] == 1


def test_two_separate_runs_are_two_onsets(tmp_path):
    rows = _quiet(5) + [_s(300, 30), _s(360, 30)] + _quiet(5, start=420) \
        + [_s(720, 50)] + _quiet(5, start=780)
    got = R.onsets(R.read(_log(tmp_path, rows)))
    assert [o["peak"] for o in got] == [30, 50]


def test_a_run_that_spans_a_hole_is_one_onset_and_says_so(tmp_path):
    """**欠測をまたいだ発端を2件に割ると、継続時間が半分になって報告される。**
    1件のまま、ただし内部が一部未観測であることを明示する。"""
    rows = _quiet(5) + [_s(300, 30)] + [_s(300 + 3600, 35)] + _quiet(5, start=300 + 3660)
    got = R.onsets(R.read(_log(tmp_path, rows)))
    assert len(got) == 1
    assert got[0]["spans_a_gap"] is True
    assert "spans a gap" in R.report(_log(tmp_path, rows))


def test_the_threshold_is_the_rule_not_the_data(tmp_path):
    """実データのピークは4で、閾値は8。**観測された範囲に合わせて閾値を決めたら、
    この計器は自分が見たものしか見つけられなくなる。**"""
    rows = _quiet(5, pages=4)
    assert R.onsets(R.read(_log(tmp_path, rows)), warn_at=8) == []
    assert len(R.onsets(R.read(_log(tmp_path, rows)), warn_at=3)) == 1


# ───────────────────────────── 「起きていない」の裏づけ

def test_a_quiet_log_reports_its_coverage_with_the_verdict(tmp_path):
    """**サンプラが止まっていたログと、ブラウザが健全だったログは同じ形。**
    被覆率を併記しない「何も起きていない」は、ブラウザではなくサンプラについての文。"""
    out = R.report(_log(tmp_path, _quiet(100)))
    assert "no sample ever exceeded the threshold" in out
    assert "coverage" in out
    assert "holds only for the observed" in out


def test_a_hole_in_the_record_is_counted_and_located(tmp_path):
    rows = _quiet(10) + _quiet(10, start=10 * 60 + 7200)     # 2時間の欠測
    cov = R.observed_coverage(R.read(_log(tmp_path, rows)))
    assert len(cov["gaps"]) == 1
    assert 7000 < cov["gaps"][0]["seconds"] < 7400
    assert "unobserved" in R.report(_log(tmp_path, rows))


def test_one_late_tick_is_not_a_hole(tmp_path):
    """スケジューリングの揺らぎを欠測として数えたら、被覆率そのものが信用できなくなる。"""
    rows = _quiet(5) + [_s(5 * 60 + 90, 2)] + _quiet(5, start=5 * 60 + 150)
    assert R.observed_coverage(R.read(_log(tmp_path, rows)))["gaps"] == []


# ───────────────────────────── 壊れた入力

def test_rows_out_of_order_are_sorted_before_anything_is_concluded(tmp_path):
    """追記専用でも、時計が戻れば順序は崩れる。並べずに走を数えると発端が刻まれる。"""
    rows = [_s(300, 30), _s(0, 2), _s(360, 30), _s(60, 2)]
    got = R.onsets(R.read(_log(tmp_path, rows)))
    assert len(got) == 1 and got[0]["samples"] == 2


def test_a_row_without_pages_or_ts_is_not_a_sample(tmp_path):
    rows = [_s(0, 2), {"ts": 60.0, "cdp": "x"}, {"pages": 99}, _s(120, 2)]
    assert len(R.read(_log(tmp_path, rows))) == 2


def test_a_half_written_last_line_does_not_stop_the_report(tmp_path):
    p = tmp_path / "page_counts.jsonl"
    with open(str(p), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(_s(0, 2)) + "\n")
        fh.write('{"ts": 60.0, "pag')
    assert "1 samples" in R.report(str(p))


def test_an_empty_or_missing_log_says_nothing_rather_than_healthy(tmp_path):
    """**空のログを「ページ0、健全」と読んだら、止まったサンプラが合格証になる。**"""
    assert "no samples" in R.report(_log(tmp_path, []))
    assert "no samples" in R.report(str(tmp_path / "never_written.jsonl"))
