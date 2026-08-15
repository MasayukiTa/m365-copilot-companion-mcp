"""The collect-mode writer: does it record what the replay needs, or only what it already had?

The replay can only be as good as the recording, and the recording had a defect that no amount
of care in the replay could compensate for: `_settle_trace` runs inside the settle loop, the
loop ends when production accepts, and so the last recorded text was production's accepted
text. Every arm weaker than production then scores zero truncation by definition -- exactly on
the turns where production truncated, because that is when recording stopped.

These tests are about the two things that fix it: samples recorded AFTER acceptance, and turn
ids that cannot collide.
"""
from __future__ import annotations

import io
import json
import os
import tempfile

import pytest

from relay import copilot_autopilot_relay as CAR


class _Driver:
    """A driver that keeps producing text after the point production would accept."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.reads = 0

    def read_last_response(self):
        self.reads += 1
        return self.texts[min(self.reads - 1, len(self.texts) - 1)]

    def _is_stale_repeat(self, _text):
        return False


@pytest.fixture()
def trace(monkeypatch):
    path = os.path.join(tempfile.mkdtemp(prefix="settle_"), "settle_trace.jsonl")
    monkeypatch.setattr(CAR, "_SETTLE_TRACE_PATH", path)
    monkeypatch.setattr(CAR, "_SETTLE_TRACE_COLLECT", True)
    monkeypatch.setattr(CAR, "_SETTLE_TRACE_AFTER_S", 0.0)
    monkeypatch.setattr(CAR, "REPLY_SETTLE_INTERVAL_S", 0.0)
    CAR._settle_trace_started_at.clear()
    CAR._settle_trace_turn.clear()
    return path


def _rows(path):
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in io.open(path, encoding="utf-8") if l.strip()]


# ---- the label tail ----------------------------------------------------------------------

def test_recording_continues_past_the_point_production_accepted(trace):
    """本番の受理点で記録が止まると、正解ラベルは『本番が受理したテキスト』になる。
    両腕は本番より弱いので、それ以前でしか受理しない -- truncation は定義上ゼロ。"""
    drv = _Driver(["half", "half and the rest", "half and the rest"])
    CAR._settle_trace_reset(drv)
    CAR._settle_trace(drv, "stable", "half", False, 3, None)
    CAR._settle_trace_label_tail(drv)

    rows = _rows(trace)
    assert len(rows) > 1, "受理後が1件も記録されていない"
    assert rows[-1]["text"] != rows[0]["text"], \
        "受理後に伸びたテキストが記録されていない -- ラベルは本番の受理点のまま"


def test_the_tail_samples_are_marked_so_no_arm_can_decide_on_them(trace):
    """本番が既に戻った後のサンプルで受理する述語は、誰も走らせられない。"""
    drv = _Driver(["a", "ab"])
    CAR._settle_trace_reset(drv)
    CAR._settle_trace(drv, "stable", "a", False, 3, None)
    CAR._settle_trace_label_tail(drv)

    rows = _rows(trace)
    assert not rows[0].get("post_accept")
    assert all(r.get("post_accept") for r in rows[1:])


def test_the_tail_is_not_recorded_outside_collect_mode(monkeypatch, trace):
    """全ポーリングの全文トレースは重い。既定の用途はデバッグで、これを求めていない。"""
    monkeypatch.setattr(CAR, "_SETTLE_TRACE_COLLECT", False)
    drv = _Driver(["a", "ab"])
    CAR._settle_trace_label_tail(drv)
    assert drv.reads == 0


def test_a_placeholder_in_the_tail_is_skipped_rather_than_recorded_as_the_label(trace):
    """『処理中です』を正解テキストとして記録したら、全ターンが truncated になる。"""
    drv = _Driver(["処理中です。", "the real answer"])
    CAR._settle_trace_reset(drv)
    CAR._settle_trace(drv, "stable", "partial", False, 3, None)
    CAR._settle_trace_label_tail(drv)

    texts = [r["text"] for r in _rows(trace)]
    assert not any(CAR._is_processing(t) for t in texts)


def test_the_tail_never_breaks_the_turn_it_is_recording(trace):
    """トレースの失敗がターンを壊せるなら、計測が本番の障害要因になる。"""
    class Broken(_Driver):
        def read_last_response(self):
            raise RuntimeError("DOM gone")

    CAR._settle_trace_label_tail(Broken([]))       # must not raise


# ---- turn identity -----------------------------------------------------------------------

def test_two_turns_never_share_an_id(trace):
    """id(drv) は GC 後に再利用される。長いキャンペーンでは別ドライバが同じ値を持ちうる。"""
    seen = set()
    for _ in range(50):
        drv = _Driver(["x"])
        CAR._settle_trace_reset(drv)
        seen.add(CAR._settle_trace_turn[id(drv)])
        del drv
    assert len(seen) == 50, "turn_id が衝突している"


def test_a_recorded_line_carries_its_turn_id(trace):
    drv = _Driver(["x"])
    CAR._settle_trace_reset(drv)
    CAR._settle_trace(drv, "stable", "x", False, 1, None)
    assert _rows(trace)[0]["turn_id"]


def test_the_collected_shape_is_what_the_replay_refuses_to_do_without(trace):
    """記録側と再生側が別々に正しくても、要求が食い違えば何も再生できない。"""
    from relay import settle_replay as SR

    drv = _Driver(["half", "half and the rest"])
    CAR._settle_trace_reset(drv)
    CAR._settle_trace(drv, "stable", "half", False, 2, None)
    CAR._settle_trace(drv, "stable", "half", False, 3, None)
    CAR._settle_trace_label_tail(drv)

    turns = SR.load_turns(trace)                  # must not raise
    out = SR.replay(turns)
    assert out["turns"] == 1
    assert out["unlabelled_turns"] == 0
