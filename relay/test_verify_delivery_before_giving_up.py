# -*- coding: utf-8 -*-
"""A timed-out acting turn is not given up STUCK when its own output proves it was delivered.

The 2026-10-27 15:53 bundle: 90 workers went STUCK with delivery=unknown on a turn timeout while
the goal acted. The refusal hard-coded delivery="unknown" and never read the driver's own evidence
(partial_text()/_turn_answered). With verify_delivery_before_giving_up ON (default), output that
arrived means the turn reached the model -> keep waiting; no output stays "unknown" -> the careful
refuse is unchanged; OFF keeps the old hard-coded "unknown".
"""
import time

import pytest

from relay import relay_fleet as RF
from relay import settle as S
from relay import delivery_verify as DV
from relay import transport_policy as TP


class _Answers:
    def count(self):
        return 0


class _Driver:
    def __init__(self, partial="", answered=False):
        self._partial = partial
        self._turn_answered = answered

    def _answers(self):
        return _Answers()

    def _is_generating(self):
        return False

    def partial_text(self):
        return self._partial


class _Worker:
    poll = RF.RelayWorker.poll
    _saw_output_for_turn = RF.RelayWorker._saw_output_for_turn
    status = "waiting"
    per_turn_timeout_s = 240
    dwell_s = 4.0
    _count_before = 0
    transient = 0
    max_transient = 10
    last_response = ""
    turn = 1
    name = "w0"
    goal = "send an email"

    def __init__(self, partial="", answered=False):
        self.drv = _Driver(partial=partial, answered=answered)
        self._t_send = time.time() - 300
        self._last_text = None
        self._stable_since = None
        self._settle_state = S.SettleState()
        self.outcome = None
        self.reason = ""
        self.retried = False
        self.refused = None
        self.retryable_override = None
        self.checks = []
        self.notes = []

    def _capture_url(self):
        pass

    def _goal_may_act(self):
        return True

    def _effect_checker(self):
        return None

    def _timeout_resend_decision(self):
        return "refuse"

    def _retry_transient(self):
        self.retried = True
        self.transient += 1
        self.status = "ready"
        return True

    def _note_timeout(self, origin, elapsed, kind, budget_s=None):
        self.notes.append(kind)

    def _salvage_via_checks(self):
        return False

    def _refuse_resend(self, reason, delivery):
        self.refused = (reason, delivery)
        self.status, self.outcome = "stuck", "STUCK"
        self.reason = "not re-sent"

    def _decide(self, text):
        raise AssertionError("no reply exists")


def test_delivery_seen_from_output_is_delivered_only_with_output():
    assert TP.delivery_seen_from_output(True) == "delivered"
    assert TP.delivery_seen_from_output(False) == "unknown"


def test_setting_default_is_on_and_off_line_is_read(tmp_path):
    p = tmp_path / "settings.txt"
    p.write_text("", encoding="utf-8")
    assert DV.mode(str(p)) == "on"            # absent key -> on
    p.write_text("verify_delivery_before_giving_up=off\n", encoding="utf-8")
    assert DV.mode(str(p)) == "off"
    assert DV.verify_on(str(p)) is False
    p.write_text("verify_delivery_before_giving_up=on\n", encoding="utf-8")
    assert DV.verify_on(str(p)) is True


def test_delivered_output_waits_instead_of_stuck(monkeypatch):
    # Setting ON and real output arrived for this turn -> the worker keeps waiting, not STUCK.
    monkeypatch.setattr(DV, "verify_on", lambda path=None: True)
    w = _Worker(partial="the model had begun writing...")
    assert w.poll() is False
    assert w.refused is None
    assert w.outcome != "STUCK"
    assert w.retried is False
    assert "delivered-wait" in w.notes


def test_answered_flag_also_counts_as_delivered(monkeypatch):
    monkeypatch.setattr(DV, "verify_on", lambda path=None: True)
    w = _Worker(partial="", answered=True)
    assert w.poll() is False
    assert w.refused is None
    assert w.outcome != "STUCK"


def test_no_output_keeps_unknown_refusal(monkeypatch):
    # Setting ON but nothing arrived -> honest unknown, the careful refuse is unchanged.
    monkeypatch.setattr(DV, "verify_on", lambda path=None: True)
    w = _Worker(partial="", answered=False)
    assert w.poll() is True
    assert w.refused is not None
    assert w.refused[1] == "unknown"
    assert w.outcome == "STUCK"


def test_setting_off_keeps_old_hardcoded_unknown(monkeypatch):
    # Even with output present, OFF keeps the old hard-coded delivery="unknown" refusal.
    monkeypatch.setattr(DV, "verify_on", lambda path=None: False)
    w = _Worker(partial="output that would have proved delivery")
    assert w.poll() is True
    assert w.refused is not None
    assert w.refused[1] == "unknown"
    assert w.outcome == "STUCK"
