# -*- coding: utf-8 -*-
"""最初の拒否回復が起きたとき、誰かが気づけるようにする。

常設の判断はこうだった（正しい）: `recovery_cause` / `recovery_result` /
`recovery_state` の読み手を**今は作らない。読むものがまだ無いから**。
2026-09-20 に2つの方法で確認済み — 549ファイル・1,574件の worker 記録で0件、
`.fleet` と `output` の全域 grep でも4パターンとも0件。機構は既定 off で、
有効にするのは `bench/review_run.py` のレビュー実行だけ。

そして続けてこう書いてあった: **「最初の非空の値が出たら、そこが読み手を作る時」**。

## その瞬間を、誰も見ていなかった

3層を運ばれて最終スナップショットに入る欄で、**UI もスクリプトも解析も読んでいない**。
つまり最初の本物の回復は、誰も開かないファイルに着地し、メモは「まだ」と言い続ける。
**変化しか報告しない監視器は死を見られない** — ここでは誕生を見られない。

だから読み手は作らない。**合図だけ作る。** 原因が実際に生成されたときに限り、
実際に読まれている台帳（`mechanisms.jsonl`）へ1行。
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


class _W:
    """`_apply_diagnosis` だけ本物から借りる。"""

    def __init__(self):
        from relay import relay_fleet as RF

        self.name = "w1"
        self.run_id = "r-test"
        self.turn = 2
        self.max_fresh_replays = 1
        self.recovery_cause = ""
        self.recovery_result = ""
        self.reason = ""
        self._fn = RF.RelayWorker._apply_diagnosis

    def apply(self, **kw):
        return self._fn(self, **kw)


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    from relay import mechanism_telemetry as MT

    monkeypatch.setattr(MT, "LOG", str(tmp_path / "mechanisms.jsonl"))
    return lambda: MT.load(str(tmp_path / "mechanisms.jsonl"))


def test_a_real_recovery_leaves_a_row_where_someone_looks(ledger):
    """**これが合図。** 最初の非空の原因が、読まれている台帳に現れる。"""
    w = _W()
    w.apply(fresh_was_refusal=False, fresh_succeeded=True, fresh_was_transient_error=False)
    assert w.recovery_cause, "the diagnosis produced nothing, so there is no moment to catch"
    rows = ledger()
    assert len(rows) == 1
    assert rows[0]["mechanism"] == "refusal_recovery"
    assert rows[0]["extra"]["cause"] == w.recovery_cause


def test_the_row_carries_the_answer_and_not_just_the_fact(ledger):
    """原因だけでは「どの答えだったか」が分からない。4つのうちどれかが本体。"""
    w = _W()
    w.apply(fresh_was_refusal=True, fresh_succeeded=False, fresh_was_transient_error=False)
    r = ledger()[0]
    assert r["extra"]["cause"] and r["extra"]["result"]


def test_the_mechanism_is_registered():
    """未登録の機構は書かれても要約に出ない = 読み手のいない計器。合図として無意味になる。"""
    from relay import mechanism_telemetry as MT

    assert "refusal_recovery" in MT.MECHANISMS


def test_a_ledger_that_fails_cannot_fail_the_settle(monkeypatch):
    """**記録は、それが説明している settle を落とせてはならない** —
    `_apply_diagnosis` の docstring がそう書いている。"""
    from relay import relay_fleet as RF

    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(RF._mt, "record", boom)
    w = _W()
    w.apply(fresh_was_refusal=False, fresh_succeeded=True, fresh_was_transient_error=False)
    assert w.recovery_cause, "the record's failure swallowed the diagnosis itself"


def test_nothing_is_recorded_when_no_cause_was_produced(ledger, monkeypatch):
    """**合図が鳴りっぱなしなら合図ではない。** 診断が原因を出さなければ黙る。"""
    from relay import relay_fleet as RF

    monkeypatch.setattr(RF, "diagnose_after_fresh_replay",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("no diagnosis")))
    w = _W()
    w.apply(fresh_was_refusal=False, fresh_succeeded=True, fresh_was_transient_error=False)
    assert ledger() == []
