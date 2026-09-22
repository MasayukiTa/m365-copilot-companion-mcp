# -*- coding: utf-8 -*-
"""調査の予算が断ったことを、台帳に残す。

`effort` は起動時に1回 `configured=True` と書かれ、**適格性の段を書く者が誰もいなかった**。
実測（2026-09-22）: 416 レコード・configured 416・eligible 0。
funnel はそれを「no opportunity（ここでは起きない問題を解いている）」と報告していた。

**実際には発火していた。** 記録済みのフリートターン 34,594 件のうち、
`RESEARCH:` が 5 件・`ANALYZE:` が 13 件、ワーカー10体にまたがる。
そのうち**1体は上限3に対して5回要求**し、もう1体はちょうど上限に達している。
つまり予算は本番で断っており、台帳には何も残っていなかった。

だから「did the budget ever bite?」に答えるのに、transcript を掘る必要があった。
**それが計器の役目**で、掘らないと分からないなら計器が欠けている。
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


class _W:
    """`_record_effort_budget` だけを本物から借りる。RelayWorker 全体を建てずに、
    この段が見ている状態（予算と使用数）だけを持たせる。"""

    def __init__(self, used, cap=3):
        from relay import relay_fleet as RF

        self.research_count = used
        self.max_research = cap
        self.max_refute = 2
        self.name = "w1"
        self.run_id = "r-test"
        self.turn = 4
        self._fn = RF.RelayWorker._record_effort_budget

    def record(self, kind):
        return self._fn(self, kind)


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    from relay import mechanism_telemetry as MT

    monkeypatch.setattr(MT, "LOG", str(tmp_path / "mechanisms.jsonl"))
    return lambda: MT.load(str(tmp_path / "mechanisms.jsonl"))


def test_a_request_within_budget_is_recorded_as_an_opportunity_not_a_firing(ledger):
    """**機会が来たことと、断ったことは別。** 委譲が通ったのは予算が達していないから。"""
    _W(used=0).record("research")
    rows = ledger()
    assert len(rows) == 1
    r = rows[0]
    assert r["mechanism"] == "effort"
    assert r["eligible"] is True, "the opportunity was not recorded, which is the old defect"
    assert r["triggered"] is False
    assert "within budget" in (r["not_triggered_reason"] or "")


def test_the_refusal_is_what_counts_as_firing(ledger):
    """**この機構は上限そのもの。** 発火とは断ったことで、委譲が走ったことではない。"""
    _W(used=3).record("research")
    r = ledger()[0]
    assert r["eligible"] is True and r["triggered"] is True


def test_both_kinds_share_the_one_budget(ledger):
    """research と analyze は `max_research` を共有する。別々に数えると、
    上限に達した理由が読めなくなる。"""
    w = _W(used=2)
    w.record("research")
    w.record("analyze")
    rows = ledger()
    assert [r["extra"]["kind"] for r in rows] == ["research", "analyze"]
    assert all(r["extra"]["used"] == 2 for r in rows)


def test_the_funnel_no_longer_calls_it_a_missing_opportunity(ledger):
    """**元の誤読を、直接は言わせない。** 適格性が書かれていれば
    `stops_at` は "not assessed" でも "no opportunity" でもなくなる。"""
    from relay import mechanism_telemetry as MT

    _W(used=3).record("research")
    f = MT.funnel(ledger(), "effort")["effort"]
    assert f["eligible"] == 1
    assert f["eligibility_undetermined"] == 0
    assert f["stops_at"] not in ("not assessed", "no opportunity"), f["stops_at"]


def test_a_broken_ledger_cannot_stop_the_worker(monkeypatch):
    """記録が機構を失敗させられてはならない — 同じ契約が `_decide` の隣にも書いてある。"""
    from relay import relay_fleet as RF

    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(RF._mt, "record", boom)
    _W(used=0).record("research")        # must not raise
