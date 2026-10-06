# -*- coding: utf-8 -*-
"""ツール経路が壊れていると宣言する停止は、上書きされない場所にも残す。

返信にツール呼び出しの記述があるのに**1件もゲートウェイに届かない**ターンが2回続くと、
worker は `INFRA_STUCK` で止まる。これは「タスクが失敗した」ではなく
**「ツール経路が壊れている」**という宣言で、意味がまるで違う。

## 一度も発火していない

実測 2026-09-22: 恒久記録 `history.json` 78件・`status.json` 470行を走査して、
`INFRA_STUCK` **0件**、この停止理由の文字列も **0件**。

## 読み手ではなく合図

`reason` は次のスイープが上書きするライブスナップショットの欄なので、
**一度出て消える**のが一番ありそうな形。読まれている台帳に1行残す。

## この検査の形について

最初はこの分岐を**書き写した**小さな偽実装を走らせていた。写しは本番の `try/except` を
落としていて、そのせいで「台帳が壊れても停止は起きる」の検査が落ちた —
**写しを検査しても、写しのことしか分からない。** だから分岐の構造は
ソースの該当箇所に錨を打って見て、実行するのは本物の `record` が
登録済みの機構として要約に出ることだけにした。
"""
from __future__ import annotations

import io
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

_SRC = io.open(os.path.join(REPO, "relay", "relay_fleet.py"), encoding="utf-8").read()


def _branch():
    """`unlanded_calls` を記録する箇所の前後だけ。ファイル全体を見ない。"""
    # The window has to reach past the record to the `except` and the `return` that close the
    # branch -- at 400 characters it stopped inside the call's own arguments and two checks
    # failed on their own slice rather than on the code.
    i = _SRC.index('_mt.record("unlanded_calls"')
    return _SRC[_SRC.rindex("if self._unlanded_calls >= 2:", 0, i):i + 900]


def test_the_mechanism_is_registered():
    """**未登録の機構は書かれても要約に出ない** — `record` の隣にそう書いてある。
    出ないなら、合図として無意味。"""
    from relay import mechanism_telemetry as MT

    assert "unlanded_calls" in MT.MECHANISMS


def test_the_registered_name_actually_summarises(tmp_path, monkeypatch):
    """登録の意味を**実行して**確かめる: funnel に現れ、発火として数えられる。"""
    from relay import mechanism_telemetry as MT

    monkeypatch.setattr(MT, "LOG", str(tmp_path / "m.jsonl"))
    MT.record("unlanded_calls", run_id="r", instance="w1", configured=True,
              eligible=True, triggered=True, executed=True, extra={"consecutive": 2})
    f = MT.funnel(MT.load(str(tmp_path / "m.jsonl")), "unlanded_calls")["unlanded_calls"]
    assert f["records"] == 1 and f["triggered"] == 1
    assert f["stops_at"] == "changed nothing"      # executed, and it changes no decision


def test_the_record_sits_inside_the_stop_and_before_it_returns():
    """**分岐は書いて即座に抜ける。** 記録が `return` の後ろにあれば一度も書かれない。"""
    b = _branch()
    assert 'self.outcome = "stuck", "INFRA_STUCK"' in b or "INFRA_STUCK" in b
    assert b.index('_mt.record("unlanded_calls"') < b.index("return")


def test_the_record_cannot_take_the_stop_down_with_it():
    """記録が機構を失敗させられてはならない — このファイルの他の記録と同じ契約。
    偽実装で確かめると写しの性質しか見えないので、実物の構造を見る。"""
    b = _branch()
    i = b.index('_mt.record("unlanded_calls"')
    before = b[:i]
    assert before.rstrip().endswith("try:"), before[-120:]
    assert "except Exception:" in b[i:], "an unwritable ledger would propagate out of the stop"


def test_two_turns_are_required_and_not_one():
    """**1回は事故、2回は経路。** 閾値が1に下がれば、一度の言い間違いで
    「インフラ障害」を宣言することになる。"""
    assert "if self._unlanded_calls >= 2:" in _SRC
