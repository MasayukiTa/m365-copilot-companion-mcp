# -*- coding: utf-8 -*-
"""A locked refuter is a harness recovery problem, never evidence that the work was upheld.

Production incident 2026-10-01:
- main worker session 41ae... was refused, then unlocked successfully;
- independent refuter opened another MCP session d578... and was refused there;
- reviewer returned INCONCLUSIVE because it could not inspect the PPTX;
- one-lens aggregate converted that to UPHELD and the run reported refuter#1: UPHELD.

The refuter must reactively unlock only AFTER its own review says lock blocked evidence.  This
keeps the 2026-09-25 proactive-turn-1 DLP fix intact while avoiding a false reviewed-and-upheld
claim.
"""
from __future__ import annotations

import time

from relay import refuter as R
from relay import settle as S


class _Answers:
    def __init__(self, drv):
        self.drv = drv
    def count(self):
        return self.drv.answer_count


class _Drv:
    def __init__(self, responses):
        self.responses = list(responses)
        self.answer_count = 1
        self.sent = []
        self.accepted = []
        self._count_before = 0
        self.failed = ""
    def _answers(self):
        return _Answers(self)
    def read_last_response(self):
        return self.responses[min(max(self.answer_count - 1, 0), len(self.responses) - 1)]
    def _is_stale_repeat(self, _text):
        return False
    def _accept_new_reply(self, text):
        self.accepted.append(text)
    def send(self, text, **_kw):
        self.sent.append(text)
        self.answer_count += 1
    def close(self):
        pass


def _session(responses):
    s = R.RefuterSession(object(), "https://agent/impl", "goal", "candidate DONE",
                         timeout_s=10_000, max_nudges=0)
    s.drv = _Drv(responses)
    s.socket = True
    s.page = None
    s._pending_open = False
    s._socket_tried = True
    s._count_before = 0
    s._t_send = time.time() - 10
    s._last = None
    s._stable_since = None
    s._settle_state = S.SettleState()
    return s


def test_only_an_unclear_or_inconclusive_lock_block_is_unlock_recovery():
    assert R.review_verdict_was_lock_blocked(
        "INCONCLUSIVE", "実行系がロックされ対象pptxを一次情報で検証できない")
    assert R.review_verdict_was_lock_blocked(
        "UNCLEAR", "harness could not continue: [locked: no valid unlock token]")
    assert not R.review_verdict_was_lock_blocked("UPHELD", "lock handling itself looks correct")
    assert not R.review_verdict_was_lock_blocked("REFUTED", "unlock path has a security defect")
    assert not R.review_verdict_was_lock_blocked("INCONCLUSIVE", "the source data is missing")


def test_locked_review_reacts_with_same_session_unlock_then_review_resume(monkeypatch):
    import relay.relay_fleet as RF
    monkeypatch.setattr(RF, "_unlock_password", lambda: "unit-test-password")
    monkeypatch.setattr(RF, "MAX_UNLOCK_ATTEMPTS", 4)
    s = _session(["INCONCLUSIVE: 実行系がロックされ検証できない", "UPHELD"])

    assert s._recover_locked_review("INCONCLUSIVE", "実行系がロックされ検証できない") is True
    assert s._done is None
    assert s._unlock_attempts == 1
    assert len(s.drv.sent) == 1
    sent = s.drv.sent[0]
    assert "unit-test-password" in sent
    assert "unlock" in sent.lower()
    assert "独立レビュー" in sent
    assert s._last is None and s._stable_since is None
    assert s._settle_state.stable_count == 0


def test_missing_unlock_password_is_a_harness_fault_not_upheld(monkeypatch):
    import relay.relay_fleet as RF
    monkeypatch.setattr(RF, "_unlock_password", lambda: "")
    s = _session(["INCONCLUSIVE: locked"])
    assert s._recover_locked_review("INCONCLUSIVE", "[locked: no valid unlock token]") is False
    assert s._done[0] == "UNCLEAR"
    assert s._done[1].startswith("harness: ")
    assert "unlock" in s._done[1].lower()


def test_unlock_budget_exhaustion_is_a_harness_fault_not_upheld(monkeypatch):
    import relay.relay_fleet as RF
    monkeypatch.setattr(RF, "_unlock_password", lambda: "unit-test-password")
    monkeypatch.setattr(RF, "MAX_UNLOCK_ATTEMPTS", 1)
    s = _session(["INCONCLUSIVE: locked"])
    s._unlock_attempts = 1
    assert s._recover_locked_review("INCONCLUSIVE", "[locked: no valid unlock token]") is False
    assert s._done[0] == "UNCLEAR"
    assert s._done[1].startswith("harness: ")
    assert "1" in s._done[1]


def test_one_lens_inconclusive_is_not_renamed_upheld():
    got = R.aggregate_panel([
        ("rootcause", "INCONCLUSIVE", "実行系がロックされ一次情報を検証できない")
    ])
    assert got[0] == "UNCLEAR", got
    assert "rootcause" in got[1]
    assert "ロック" in got[1]


def test_all_harness_fault_panel_is_unclear_not_upheld():
    got = R.aggregate_panel([
        ("correctness", "UNCLEAR", "harness: no page"),
        ("security", "UNCLEAR", "harness: locked after recovery"),
    ])
    assert got[0] == "UNCLEAR"
    assert "correctness" in got[1] and "security" in got[1]


def test_real_upheld_still_allows_an_unrelated_inconclusive_lens():
    got = R.aggregate_panel([
        ("correctness", "UPHELD", ""),
        ("edge", "INCONCLUSIVE", "not enough evidence on one edge case"),
    ])
    assert got[0] == "UPHELD"


class _Page:
    def goto(self, *_a, **_k):
        pass
    def wait_for_timeout(self, _ms):
        pass
    def locator(self, _sel):
        class _L:
            def count(self):
                return 1
        return _L()
    def close(self):
        pass


class _Ctx:
    def new_page(self):
        return _Page()


class _BlockingDrv:
    instances = []
    def __init__(self, _page):
        self.sent = []
        self.responses = [
            "INCONCLUSIVE: 実行系がロックされ対象pptxを一次情報で検証できない",
            "review complete\nUPHELD",
        ]
        self.__class__.instances.append(self)
    def send(self, text):
        self.sent.append(text)
    def wait_for_idle(self, timeout_s=0):
        return True
    def read_last_response(self):
        return self.responses[min(len(self.sent) - 1, len(self.responses) - 1)]


def test_blocking_refuter_uses_the_same_reactive_unlock_contract(monkeypatch):
    import relay.relay_fleet as RF
    import relay.copilot_autopilot_relay as CAR
    monkeypatch.setattr(RF, "_unlock_password", lambda: "x-test-x")
    monkeypatch.setattr(RF, "MAX_UNLOCK_ATTEMPTS", 4)
    _BlockingDrv.instances.clear()
    monkeypatch.setattr(CAR, "CopilotWebDriver", _BlockingDrv)
    got = R.run_refuter(_Ctx(), "https://agent/impl/conversation/abc", "goal", "DONE",
                        timeout_s=30, max_nudges=0)
    assert got == ("UPHELD", "")
    drv = _BlockingDrv.instances[-1]
    assert len(drv.sent) == 2
    assert "x-test-x" in drv.sent[1]
    assert "独立レビュー" in drv.sent[1]


def test_blocking_refuter_without_local_recovery_material_is_not_upheld(monkeypatch):
    import relay.relay_fleet as RF
    import relay.copilot_autopilot_relay as CAR
    monkeypatch.setattr(RF, "_unlock_password", lambda: "")
    _BlockingDrv.instances.clear()
    monkeypatch.setattr(CAR, "CopilotWebDriver", _BlockingDrv)
    got = R.run_refuter(_Ctx(), "https://agent/impl/conversation/abc", "goal", "DONE",
                        timeout_s=30, max_nudges=2)
    assert got[0] == "UNCLEAR"
    assert got[1].startswith("harness: ")
    assert len(_BlockingDrv.instances[-1].sent) == 1, "a harness lock failure is not a reviewer preamble to nudge"
