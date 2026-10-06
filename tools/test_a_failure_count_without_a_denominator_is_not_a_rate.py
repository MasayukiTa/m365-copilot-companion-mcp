# -*- coding: utf-8 -*-
"""失敗を数えるなら、走った回数も数えろ。

`.fleet/probe_failures.jsonl` は失敗したプローブを1件ずつ記録する。
**走ったプローブの総数は、どこにも数えられていなかった。**
`tool_probe.json` が持っているのは**最後の1回の結果**だけ。

だから500行は「**分母のない分子**」だった。率でもなく、傾向でもなく、総数ですらない
— この台帳は500件で刈られるので、500は**上限**であって件数ではない。
実測（2026-09-22）: 500行 / 08-24〜09-22 / error 272・timeout 69・consent_card 67・
stale_repeat 52・agent_unreachable 40。**この内訳を読んでも、健全なのか壊れているのか
分からない。**

`send_failures.jsonl` で同じ形を潰している: 分母を与えたことで、本番の規則が引く
「28/72」が**全体では 0.19%** だと分かった。

## 3つ数える。1つではなく

`probes` は試行、`failures` はその部分集合、`since_ts` は**その比が何日分か**。
最後が無いと、読み手は比を手にして、それが一日の話か一か月の話かを知らない。
"""
from __future__ import annotations

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import tool_probe as TP  # noqa: E402


def _totals(path):
    with open(str(path), encoding="utf-8") as fh:
        return (json.load(fh) or {}).get("totals")


def test_a_probe_is_counted_whether_it_passed_or_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(TP, "_PROBE_FILE", tmp_path / "tool_probe.json")
    TP.record_probe(True, "answer", "fine")
    TP.record_probe(False, "error", "broken")
    TP.record_probe(True, "answer", "fine")
    t = _totals(tmp_path / "tool_probe.json")
    assert t["probes"] == 3, "the denominator is missing, so the failures are a numerator alone"
    assert t["failures"] == 1


def test_the_window_the_ratio_covers_is_recorded(tmp_path, monkeypatch):
    """**比だけ渡すのは、答えの半分を伏せること。** 一日の 1/3 と一か月の 1/3 は別の話。"""
    monkeypatch.setattr(TP, "_PROBE_FILE", tmp_path / "tool_probe.json")
    TP.record_probe(True, "answer", "", ts=1_000.0)
    TP.record_probe(False, "error", "", ts=9_000.0)
    t = _totals(tmp_path / "tool_probe.json")
    assert t["since_ts"] == 1_000.0, "the window start was reset by a later write"


def test_the_counts_survive_the_write_that_replaces_the_file(tmp_path, monkeypatch):
    """この書き込みはファイルを**置き換える**。繰り越さなければ、常に1で始まる。"""
    monkeypatch.setattr(TP, "_PROBE_FILE", tmp_path / "tool_probe.json")
    for _ in range(5):
        TP.record_probe(False, "timeout", "")
    assert _totals(tmp_path / "tool_probe.json")["failures"] == 5


def test_a_probe_starting_is_not_counted_as_a_probe(tmp_path, monkeypatch):
    """**始まったプローブは結果ではない。** これを数えると、走っていない試行で
    分母が膨らみ、率が実際より健全に見える。"""
    monkeypatch.setattr(TP, "_PROBE_FILE", tmp_path / "tool_probe.json")
    TP.record_probe(True, "answer", "")
    for kind in TP._TRANSITIONAL:
        TP.record_probe(False, kind, "")
    assert _totals(tmp_path / "tool_probe.json")["probes"] == 1


def test_a_probe_starting_does_not_erase_the_counts(tmp_path, monkeypatch):
    """遷移状態の記録は**最後の判定の隣に置かれる**書き方に直されている。
    その書き込みが totals を落とすと、分母は静かに0へ戻る。"""
    monkeypatch.setattr(TP, "_PROBE_FILE", tmp_path / "tool_probe.json")
    TP.record_probe(False, "error", "")
    TP.record_probe(False, "checking", "")
    t = _totals(tmp_path / "tool_probe.json")
    assert t and t["probes"] == 1 and t["failures"] == 1


def test_every_field_a_rate_needs_comes_from_the_writer(tmp_path, monkeypatch):
    """**欄が揃っていることを、書き手に実際に書かせて確かめる。**

    最初に書いたこの検査はリテラルの辞書から率を計算していた — 算術を検査していて、
    コードを何も検査していない。今日3本目の「落ちないテスト」だったので置き換えた。
    """
    monkeypatch.setattr(TP, "_PROBE_FILE", tmp_path / "tool_probe.json")
    for i in range(10):
        TP.record_probe(i != 3, "answer" if i != 3 else "error", "", ts=1_000.0 + i)
    t = _totals(tmp_path / "tool_probe.json")
    assert set(t) >= {"probes", "failures", "since_ts"}, t
    assert 100.0 * t["failures"] / t["probes"] == 10.0
    assert t["since_ts"] == 1_000.0
