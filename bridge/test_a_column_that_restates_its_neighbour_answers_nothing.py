# -*- coding: utf-8 -*-
"""隣の列の言い換えは、列ではない。

`outer_read.jsonl` の `final_is_proc` は `_is_proc(final)` で、
**`_is_proc("")` は設計どおり True** — 本文が空なら「まだ続いている」で、
ストリームを止める判定としてはそれが正しい。

**記録の列としては、2つの別の事実を1つに潰す。** 「本文が無かった」と
「処理中マーカーに当たった」が同じ値になる。

実測（2026-09-20、13,694行）: **全行で `final_is_proc == (final_len == 0)`**。
本物のマーカー一致だった行は**0件**。3か月分の遅いターンで、この経路から
`PROCESSING_MARKERS` の枝は一度も発火していない。
**ここが効いていると思ってマーカーを調整した人は、届かない枝を調整していた。**

古い列は残す（過去の行は過去の意味を持つ）。`final_marker_hit` が、
その列が答えるはずだった問い — **存在する本文にマーカーが当たったか**。
"""
from __future__ import annotations

import io
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import bridge.copilot_bridge as B  # noqa: E402


@pytest.fixture()
def trace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(B, "_SETTLE_RESET_TRACE_AFTER_S", 0.0)

    def rows():
        p = tmp_path / ".fleet" / "outer_read.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in io.open(p, encoding="utf-8") if l.strip()]

    return rows


def test_no_text_is_not_a_marker_hit(trace):
    """**欠陥そのもの。** 13,694行すべてがこの形だった。"""
    B._outer_read_trace(0.0, "", "", "")
    r = trace()[0]
    assert r["final_len"] == 0
    assert r["final_is_proc"] is True, "the old column keeps meaning what it meant"
    assert r["final_marker_hit"] is False, "empty text was counted as a marker hit"


def test_a_real_marker_on_real_text_is_one(trace):
    marker = B.PROCESSING_MARKERS[0]
    B._outer_read_trace(0.0, "", marker, "")
    r = trace()[0]
    assert r["final_len"] > 0
    assert r["final_marker_hit"] is True


def test_an_ordinary_answer_is_neither(trace):
    B._outer_read_trace(0.0, "", "here is the answer you asked for", "")
    r = trace()[0]
    assert r["final_is_proc"] is False and r["final_marker_hit"] is False


def test_the_two_columns_can_now_disagree(trace):
    """**この一行が、直したことの全部。** 以前はどの行でも一致していた。"""
    B._outer_read_trace(0.0, "", "", "")
    r = trace()[0]
    assert r["final_is_proc"] != r["final_marker_hit"]


def test_the_row_can_be_placed_in_time(trace):
    """他の台帳と同じ理由で `ts` を足した — 経過秒だけでは何とも突き合わせられない。"""
    import time

    before = time.time()
    B._outer_read_trace(before - 30.0, "", "", "")
    r = trace()[0]
    assert r["ts"] >= before and r["age_s"] >= 30.0


def test_a_fast_turn_still_writes_nothing(trace, monkeypatch):
    """**この計器は遅いターンだけを見る。** 静かなままであることは性質の一部。"""
    import time

    monkeypatch.setattr(B, "_SETTLE_RESET_TRACE_AFTER_S", 20.0)
    B._outer_read_trace(time.time() - 0.5, "", "", "")
    assert trace() == []
