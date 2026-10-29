# -*- coding: utf-8 -*-
"""成果物を書き終えたあとのトークン期限切れが STUCK にならないことを固定する。

背景 (relay/relay_fleet.py:8880 _fall_back_to_tab):
  #360 は「期限切れで拒否されたターンを即再取得」を入れたが、
  _fall_back_to_tab の _may_have_landed 判定の 3 項目のうち
  self._journal_shows_executed_calls() だけが delivery_proves_not_sent(期限切れ)
  のガードを欠いていた。continue-turn 設定 ON で、成果物を書き終えた
  (= journal に実行済み呼び出しが残る) 前ターンのあとにトークが期限切れ
  すると、executed=True になり、acting 目標なので _refuse_resend → STUCK に
  なっていた (ジョブ 26ce2fe9fec7 / 走行 r6ae36308_a0_w0, 2026-10-30 00:55)。

  期限切れ (delivery_proves_not_sent が True) はフレームがサーバーに到達して
  いないことの証明なので、このターンはまだ届いておらず、新しいトークンで
  続きを送れば良い。STUCK にすべきではない。_retry_socket は 8777-8792 で
  `if not _not_sent` ガードを持ち既にこれを救済しているが、_fall_back_to_tab の
  journal 項だけがその対称性を欠いていた。

本ファイルは本物のフリートもネットワークも使わない。
既存 relay/test_an_expired_token_is_not_a_refusal.py の偽ドライバ・偽ルートの仕組みを
そのまま再利用する。
"""
from types import SimpleNamespace

import pytest

from relay import relay_fleet as RF


EXPIRED = ("ChatHubError: the supplied token has expired. "
           "Ask the browser for a fresh one and retry.")


class _Drv:
    def __init__(self, reason):
        self.failed = reason

    def partial_text(self):
        return ""

    def conversation_ids(self):
        return {"server": "conv-1"}

    def close(self):
        pass


def _route(events):
    return SimpleNamespace(
        record=lambda event, **f: events.append((event, f)),
        open=lambda: True,
        needs_refresh=lambda: False,
        token_life=lambda: 0,
        default_agent_url=None,
    )


def _make(reason, *, executed, acting=True):
    w = SimpleNamespace()
    w.drv = _Drv(reason)
    w.name = "w0"
    w.turn = 3
    # 実ジョブと同様の acting 目標 (「作成」「保存」「push」を含む)。
    w.goal = "レポートを作成してください。commit して push してください。"
    w._landed_pending = False
    w.refused = None
    w.status = "running"
    w._needs_socket_rebind = False
    w._agent_url = None
    w._socket_rebinds = 0
    w._socket_turns_seen = 0
    w._goal_may_act = lambda: acting
    w._journal_shows_executed_calls = lambda: executed
    # effect_checker はなし (mail と同じく観測不能扱い = resend は許されない側)。
    w._effect_checker = lambda: None

    def _refuse_resend(reason, delivery):
        w.refused = (reason, delivery)
        w.status, w.outcome = "stuck", "STUCK"
        w.reason = ("not re-sent: the turn may already have been delivered and this goal "
                    "performs an action (delivery=%s, %s)" % (delivery, str(reason)[:120]))
    w._refuse_resend = _refuse_resend
    return w


def _bind(monkeypatch, events):
    monkeypatch.setattr(RF, "_socket_route", lambda: _route(events))
    monkeypatch.setattr(RF, "report_route_fault", lambda *a, **k: None)
    monkeypatch.setattr(RF, "_consider_socket_refresh", lambda *a, **k: None)


def test_fall_back_to_tab_expired_with_executed_call_is_not_refused(monkeypatch):
    """_fall_back_to_tab: 前ターンで成果物を書き終え (executed=True) + トークン期限切れ。
    フレームは送られておらず、このターンは未届。STUCK にせず、ルート待ちで保持する。"""
    events = []
    _bind(monkeypatch, events)
    w = _make(EXPIRED, executed=True)
    result = RF.RelayWorker._fall_back_to_tab(w)
    assert result is True, "期限切れはルート待ちで保持されるはず (True)"
    assert w.refused is None, "期限切れ（フレーム未送信）なのに再送を拒否した"
    assert getattr(w, "outcome", None) != "STUCK", "成果物完成済みでも期限切れで STUCK にしてはいけない"
    assert w._needs_socket_rebind is True
    assert w.status == "ready"


def test_fall_back_to_tab_delivered_executed_act_is_still_refused(monkeypatch):
    """対照: 届いたことが示される reason (carried no text) + executed + acting は
    従来どおり refuse。期限切れの救済がこのカードを広げすぎていないことの固定。"""
    events = []
    _bind(monkeypatch, events)
    w = _make("the turn completed but carried no text", executed=True)
    result = RF.RelayWorker._fall_back_to_tab(w)
    assert result is False
    assert w.refused is not None, "届いた acting ターンの再送は従来どおり refuse するはず"
    assert w.status == "stuck"
