# -*- coding: utf-8 -*-
"""Fresh M365 chats must not acknowledge the transient empty-draft window as a send."""
from __future__ import annotations

import time

from relay import copilot_autopilot_relay as relay


class _Keyboard:
    def __init__(self):
        self.inserted = []
        self.pressed = []
    def press(self, key):
        self.pressed.append(key)
    def insert_text(self, text):
        self.inserted.append(text)


class _Page:
    def __init__(self):
        self.keyboard = _Keyboard()
        self.waits = []
    def wait_for_timeout(self, ms):
        self.waits.append(ms)
        time.sleep(ms / 1000.0)


class _Composer:
    def __init__(self):
        self.clicks = 0
    def click(self, **kwargs):
        self.clicks += 1


def test_fresh_composer_reinserts_after_hydration_reset_and_requires_stability(monkeypatch):
    page = _Page()
    driver = relay.CopilotWebDriver(page)
    composer = _Composer()
    wanted = "fresh goal"
    # First read looks correct, then hydration wipes it. After reinsertion it stays correct.
    seen = iter([wanted, "", wanted, wanted, wanted, wanted, wanted])
    monkeypatch.setattr(driver, "_composer_text", lambda: next(seen, wanted))
    ok = driver._stabilize_fresh_composer(composer, wanted, stable_s=0.12, timeout_s=1.0)
    assert ok is True
    assert page.keyboard.inserted == [wanted], "the reset draft must be reinserted before Send"
    assert "Control+a" in page.keyboard.pressed and "Delete" in page.keyboard.pressed
    assert composer.clicks >= 1


def test_fresh_user_receipt_accepts_exact_intended_turn(monkeypatch):
    driver = relay.CopilotWebDriver(_Page())
    seq = iter([[], ["You said: fresh goal"]])
    monkeypatch.setattr(driver, "_visible_user_questions", lambda: next(seq, ["You said: fresh goal"]))
    assert driver._wait_fresh_user_receipt("fresh goal", 0, timeout_s=.5, mismatch_settle_s=.05)


def test_fresh_user_receipt_rejects_empty_first_turn_even_if_a_later_resend_matches(monkeypatch):
    driver = relay.CopilotWebDriver(_Page())
    seq = iter([
        ["You said:   "],
        ["You said:   "],
        ["You said:   ", "You said: fresh goal"],
    ])
    monkeypatch.setattr(driver, "_visible_user_questions", lambda: next(seq, ["You said:   ", "You said: fresh goal"]))
    assert not driver._wait_fresh_user_receipt("fresh goal", 0, timeout_s=.5, mismatch_settle_s=.05)


def test_fresh_receipt_requires_one_new_marker_bearing_user_turn_not_url_or_generation_only():
    src = open(relay.__file__, encoding="utf-8").read()
    send = src[src.index("    def send(self, text:"):src.index("    def wait_for_idle", src.index("    def send(self, text:"))]
    assert 'user_count_before = len(self._visible_user_questions())' in send
    assert 'self._stabilize_fresh_composer(composer, one_line)' in send
    assert 'self._wait_fresh_user_receipt(one_line, user_count_before' in send
    # Fresh success must not return merely because the URL changed or Stop/generating appeared.
    fresh = send[send.index('if not fresh_conversation:'):]
    assert 'if "/conversation/" in current_url.lower() or self._is_generating():\n                            return' not in fresh
