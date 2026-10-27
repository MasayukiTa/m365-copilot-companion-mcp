# -*- coding: utf-8 -*-
"""An expired token (or an unopened socket) is NOT a resend refusal.

A delivery-before-the-wire failure (expired token / unopened socket) proves the frame never
reached the server, so it is not a refusal: the turn is still owed and is re-sent once
a fresh token arrives; while none is available the worker is held (token wait), not stuck.
This covers all three _refuse_resend sites that an expired-token reason can reach.
"""
from types import SimpleNamespace

import pytest

from relay import relay_fleet as RF
from relay.transport_policy import delivery_proves_not_sent


EXPIRED = ("ChatHubError: the supplied token has expired. "
           "Ask the browser for a fresh one and retry.")
UNOPENED = "could not open the socket: handshake timeout"


def test_expired_token_proves_not_sent():
    assert delivery_proves_not_sent(EXPIRED) is True


def test_unopened_socket_proves_not_sent():
    assert delivery_proves_not_sent(UNOPENED) is True


def test_delivered_or_unknown_do_not_prove_not_sent():
    for reason in ("ConnectionClosed: 1006 after output",
                   "keepalive timeout",
                   "deadline exceeded",
                   ""):
        assert delivery_proves_not_sent(reason) is False, reason


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


def _make(reason, *, executed, plan_continue=None):
    w = SimpleNamespace()
    w.drv = _Drv(reason)
    w.name = "w0"
    w.turn = 1
    w.goal = "send an email"
    w._landed_pending = False
    w.refused = None
    w.status = "running"
    w._needs_socket_rebind = False
    w._agent_url = None
    w._goal_may_act = lambda: True
    w._journal_shows_executed_calls = lambda: executed
    w._effect_checker = lambda: None
    w._plan_continue = lambda reason, delivery: plan_continue
    w._refuse_continue = lambda reason: None

    def _refuse_resend(reason, delivery):
        w.refused = (reason, delivery)
        w.status, w.outcome = "stuck", "STUCK"
        w.reason = "not re-sent"
    w._refuse_resend = _refuse_resend
    return w


def _bind(monkeypatch, events):
    monkeypatch.setattr(RF, "_socket_route", lambda: _route(events))
    monkeypatch.setattr(RF, "report_route_fault", lambda *a, **k: None)
    monkeypatch.setattr(RF, "_consider_socket_refresh", lambda *a, **k: None)


def test_retry_socket_expired_with_executed_call_is_not_refused(monkeypatch):
    # _retry_socket: executed call this side + expired token. The frame never left, so this
    # must NOT refuse; it returns False to fall through to the wait/rebind path.
    events = []
    _bind(monkeypatch, events)
    w = _make(EXPIRED, executed=True, plan_continue=None)
    result = RF.RelayWorker._retry_socket(w, EXPIRED)
    assert result is False
    assert w.refused is None
    assert getattr(w, "outcome", None) != "STUCK"


def test_fall_back_to_tab_expired_is_held_not_refused(monkeypatch):
    events = []
    _bind(monkeypatch, events)
    w = _make(EXPIRED, executed=False)
    result = RF.RelayWorker._fall_back_to_tab(w)
    assert result is True
    assert w.refused is None
    assert w._needs_socket_rebind is True
    assert w.status == "ready"


def test_fall_back_to_tab_delivered_landed_act_is_still_refused(monkeypatch):
    events = []
    _bind(monkeypatch, events)
    w = _make("the turn completed but carried no text", executed=False)
    w._landed_pending = True
    result = RF.RelayWorker._fall_back_to_tab(w)
    assert result is False
    assert w.refused is not None
    assert w.status == "stuck"


def test_rebind_holds_an_expired_token_worker_as_token_wait(monkeypatch):
    class _Hold:
        def hold_reason(self, *a, **k):
            return "token wait"
        def release(self, *a, **k):
            pass
    fake_tp = SimpleNamespace(
        hold=lambda: _Hold(),
        wait_reason=lambda *a, **k: "token wait",
    )
    import sys
    monkeypatch.setitem(sys.modules, "relay.token_provider", fake_tp)
    monkeypatch.setattr(RF, "_socket_route",
                        lambda: SimpleNamespace(driver_for=lambda *a, **k: None))
    w = SimpleNamespace()
    w.name = "w0"
    w._socket_conv_id = "conv-1"
    w._agent_url = None
    w.reason = ""
    w.status = "running"
    held = RF.RelayWorker._rebind_socket(w)
    assert held is False
    assert w.status != "stuck"
    assert w.reason
