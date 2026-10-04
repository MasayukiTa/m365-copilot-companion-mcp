# -*- coding: utf-8 -*-
import time
from types import SimpleNamespace

from relay import relay_fleet as RF
from relay import settle as S


class _Answers:
    def count(self): return 0


class _Driver:
    def _answers(self): return _Answers()
    def _is_generating(self): return False


class _Worker:
    poll = RF.RelayWorker.poll
    status = 'waiting'
    per_turn_timeout_s = 240
    dwell_s = 4.0
    _count_before = 0
    transient = 0
    max_transient = 10
    last_response = ''
    turn = 1
    name = 'w0'
    goal = 'send an email'

    def __init__(self, may_act=True, resend_decision='refuse'):
        self.drv = _Driver()
        self._t_send = time.time() - 300
        self._last_text = None
        self._stable_since = None
        self._settle_state = S.SettleState()
        self.outcome = None
        self.reason = ''
        self._may_act = may_act
        self._resend_decision = resend_decision
        self.retried = False
        self.refused = None
        self.retryable_override = None
        self.checks = []

    def _capture_url(self): pass
    def _goal_may_act(self): return self._may_act
    def _effect_checker(self): return None
    def _timeout_resend_decision(self):
        return self._resend_decision if self._may_act else "resend"
    def _retry_transient(self):
        self.retried = True
        self.transient += 1
        self.status = 'ready'
        return True
    def _note_timeout(self, *a, **k): pass
    def _salvage_via_checks(self): return False
    def _refuse_resend(self, reason, delivery):
        self.refused = (reason, delivery)
        self.status, self.outcome = 'stuck', 'STUCK'
        self.reason = 'not re-sent'
    def _decide(self, text): raise AssertionError('no reply exists')


def test_action_goal_timeout_does_not_blindly_retry(monkeypatch):
    w = _Worker(may_act=True)
    assert w.poll() is True
    assert w.retried is False
    assert w.refused is not None
    assert w.refused[1] == 'unknown'
    assert w.outcome == 'STUCK'


def test_non_action_timeout_keeps_existing_transient_retry(monkeypatch):
    w = _Worker(may_act=False)
    assert w.poll() is False
    assert w.retried is True
    assert w.status == 'ready'


def test_checkably_absent_action_may_retry(monkeypatch):
    w = _Worker(may_act=True, resend_decision="resend")
    assert w.poll() is False
    assert w.retried is True
    assert w.refused is None


def test_transport_policy_import_failure_is_fail_closed_and_observable(monkeypatch, capsys):
    import builtins

    events = []
    route = SimpleNamespace(record=lambda event, **fields: events.append((event, fields)))
    monkeypatch.setattr(RF, "_socket_route", lambda: route)

    real_import = builtins.__import__
    def broken_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "relay.transport_policy" and "resend_decision_for_landed_act" in tuple(fromlist or ()):
            raise ImportError("synthetic transport policy load failure")
        return real_import(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, "__import__", broken_import)

    w = SimpleNamespace(
        goal="send an email", name="w9", turn=7,
        _goal_may_act=lambda: True, _effect_checker=lambda: None,
    )
    assert RF.RelayWorker._timeout_resend_decision(w) == "refuse"
    assert events and events[0][0] == "resend_policy_error"
    fields = events[0][1]
    assert fields["worker"] == "w9"
    assert fields["turn"] == 7
    assert fields["error_type"] == "ImportError"
    assert "synthetic transport policy load failure" not in repr(fields)
    out = capsys.readouterr().out
    assert "resend policy unavailable" in out
    assert "ImportError" in out
