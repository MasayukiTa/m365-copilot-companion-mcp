# -*- coding: utf-8 -*-
"""経過秒だけの記録は、他の何とも突き合わせられない。そして黙って止まる記録は健全に見える。

`.fleet/` の書き手を1本ずつ照合した結果、**時刻を持たない台帳が2本**あった:

    send_stage.jsonl    519 行   age_s / stage / attempt / composer_len / gw
    settle_reset.jsonl   35 行   age_s / gen_active / final_len / stable_len / ...

どちらも `time.time() - t0` を計算して `age_s` を書き、**その `time.time()` を捨てていた**。
`age_s` は「どれだけ遅かったか」に答えるが、「**いつ**」には答えない。
そして相関はすべて「いつ」から始まる — `send_failures.jsonl` の隣に置くことも、
障害の時刻に並べることも、互いに並べることすらできなかった。

**もう一つ、同じ家族の欠陥。** 両方ともファイルサイズの上限に達すると `return` して
黙る。**記録が止まった状態と、遅い送信が起きなくなった状態が、同じ形のログを残す** —
しかも後者は良い報せなので、読む側は自然にそちらを取る。`page_counts` で同じ形を
踏んでいる（被覆率97%・未観測3.6時間を併記するようにした件）。
上限に達したことを**1行だけ**書いて、沈黙の理由をファイル自身に持たせる。
"""
from __future__ import annotations

import io
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _rows(path):
    out = []
    for line in io.open(path, encoding="utf-8"):
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def test_a_slow_send_records_when_it_happened(tmp_path, monkeypatch):
    from relay import copilot_autopilot_relay as R

    p = tmp_path / "send_stage.jsonl"
    monkeypatch.setattr(R, "_SEND_STAGE_PATH", str(p))
    monkeypatch.setattr(R, "_SEND_STAGE_AFTER_S", 0.0)
    import time as _t

    before = _t.time()
    R._send_stage(before - 9.0, "gate_done", gw=1)
    rows = _rows(p)
    assert rows and "ts" in rows[0], "the row cannot be placed in time"
    assert rows[0]["ts"] >= before
    assert rows[0]["age_s"] >= 9.0, "the duration is still there; ts is in addition to it"


def test_a_fast_send_still_writes_nothing(tmp_path, monkeypatch):
    """**健全な送信は無音のまま。** 時刻を足したことで、静かだった計器が
    おしゃべりになっては意味が変わる。"""
    from relay import copilot_autopilot_relay as R

    p = tmp_path / "send_stage.jsonl"
    monkeypatch.setattr(R, "_SEND_STAGE_PATH", str(p))
    monkeypatch.setattr(R, "_SEND_STAGE_AFTER_S", 5.0)
    import time as _t

    R._send_stage(_t.time() - 0.2, "gate_done")
    assert not p.exists()


def test_the_log_says_when_it_stopped_recording(tmp_path, monkeypatch):
    """**欠陥そのもの。** 上限に達したあとの沈黙を、機械が静かになったと読めてしまう。"""
    from relay import copilot_autopilot_relay as R

    p = tmp_path / "send_stage.jsonl"
    p.write_text("x" * 2_000_050 + chr(10), encoding="utf-8")   # production always ends a line
    monkeypatch.setattr(R, "_SEND_STAGE_PATH", str(p))
    monkeypatch.setattr(R, "_SEND_STAGE_AFTER_S", 0.0)
    monkeypatch.setattr(R, "_SEND_STAGE_CAPPED_SAID", False)
    import time as _t

    R._send_stage(_t.time() - 9.0, "gate_done")
    tail = io.open(p, encoding="utf-8").read().splitlines()[-1]
    rec = json.loads(tail)
    assert rec["stage"] == "log_capped" and "ts" in rec
    assert "not a quiet machine" in rec["note"]


def test_it_says_so_once_and_not_per_send(tmp_path, monkeypatch):
    """**理由を1行で足すつもりが、上限後の全件に1行ずつ足してはいけない。**"""
    from relay import copilot_autopilot_relay as R

    p = tmp_path / "send_stage.jsonl"
    p.write_text("x" * 2_000_050 + chr(10), encoding="utf-8")   # production always ends a line
    monkeypatch.setattr(R, "_SEND_STAGE_PATH", str(p))
    monkeypatch.setattr(R, "_SEND_STAGE_AFTER_S", 0.0)
    monkeypatch.setattr(R, "_SEND_STAGE_CAPPED_SAID", False)
    import time as _t

    for _ in range(5):
        R._send_stage(_t.time() - 9.0, "gate_done")
    lines = io.open(p, encoding="utf-8").read().splitlines()
    assert sum(1 for l in lines if "log_capped" in l) == 1


def test_the_settle_trace_carries_the_time_too(tmp_path, monkeypatch):
    """**2本とも同じ欠陥だった。** 片方だけ直すのが、今日ずっと潰している形。"""
    import bridge.copilot_bridge as B
    import time as _t

    p = tmp_path / "settle_reset.jsonl"
    monkeypatch.setattr(B, "_SETTLE_RESET_TRACE_PATH", str(p))
    monkeypatch.setattr(B, "_SETTLE_RESET_TRACE_AFTER_S", 0.0)
    monkeypatch.setattr(B, "_SETTLE_RESET_CAPPED_SAID", False)

    before = _t.time()
    B._settle_reset_trace(before - 4.0, "final text", "stable text", True)
    rows = _rows(p)
    assert rows and rows[0]["ts"] >= before
    assert rows[0]["age_s"] >= 4.0


def test_the_settle_trace_says_when_it_stopped(tmp_path, monkeypatch):
    import bridge.copilot_bridge as B
    import time as _t

    p = tmp_path / "settle_reset.jsonl"
    p.write_text("x" * 10 + chr(10), encoding="utf-8")
    monkeypatch.setattr(B, "_SETTLE_RESET_TRACE_PATH", str(p))
    monkeypatch.setattr(B, "_SETTLE_RESET_TRACE_AFTER_S", 0.0)
    monkeypatch.setattr(B, "_SETTLE_RESET_TRACE_MAX_BYTES", 5)
    monkeypatch.setattr(B, "_SETTLE_RESET_CAPPED_SAID", False)

    for _ in range(3):
        B._settle_reset_trace(_t.time() - 4.0, "f", "s", False)
    lines = io.open(p, encoding="utf-8").read().splitlines()
    capped = [l for l in lines if "log_capped" in l]
    assert len(capped) == 1, lines
    assert "ts" in json.loads(capped[0])
