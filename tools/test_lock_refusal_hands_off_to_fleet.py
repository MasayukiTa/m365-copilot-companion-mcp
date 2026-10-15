# -*- coding: utf-8 -*-
"""A locked agent without the password must be told ONE thing: hand the request to fleet_submit.

MEASURED 2026-10-09, from a phone. An agent was refused by a gated tool, did not hold the
password, and ended its turn by offering the user a menu (ask for the password / submit to the
queue / use a browser) instead of calling fleet_submit. The instructions it had been given
carried two statements at once: "say so and stop" and "do not stop: hand it to fleet_submit".

This pins the instruction text, not the agent's behaviour: whether a model then obeys is a
separate question. It only guarantees the text no longer contradicts itself.
"""
import ast
import io
import os
import re
import sys
import time
from types import SimpleNamespace

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools.test_instructions_do_not_accumulate_cases import instructions_text  # noqa: E402

#: Same ceiling relay_fleet uses to decide a short return value IS a lock refusal.
DOMINANCE_MAX = 400
LONG_IPV6 = "ffff:ffff:ffff:ffff:ffff:ffff:255.255.255.255"


def _instructions():
    return instructions_text()


def _rule(text, n):
    m = re.search(r"RULE %d\b.*?(?=RULE %d\b|$)" % (n, n + 1), text, re.S)
    assert m, "RULE %d not found" % n
    return m.group(0)


def test_no_stop_instruction_coexists_with_the_fleet_handoff():
    text = _instructions()
    assert "fleet_submit" in text
    assert "say so and stop" not in text.lower()


def test_rule3_gives_one_action_when_there_is_no_password():
    rule = _rule(_instructions(), 3)
    low = rule.lower()
    assert "do not have the password" in low
    for needed in ("do not stop", "do not offer the user a choice", "fleet_submit",
                   "immediately", "queued"):
        assert needed in low, needed
    # The old intent survives: no passing read-only results off as the finished task.
    assert "as though the task were done" in low


def test_rule8_does_not_send_a_passwordless_agent_to_unlock():
    rule = _rule(_instructions(), 8)
    low = rule.lower()
    assert "have the password" in low and "once" in low
    assert "do not have it, do not call unlock" in low
    assert "fleet_submit" in low and "rule 3" in low


def _refusals(monkeypatch):
    from tools import security as sec

    class Headers:
        def get(self, key, default=""):
            return LONG_IPV6 if key.lower() == "x-forwarded-for" else default

    req = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers=Headers())
    live = {LONG_IPV6: {"expires_at": time.time() + 3600.0, "token_hashes": [], "sessions": {}}}
    monkeypatch.setattr(sec, "get_http_request", lambda: req)
    monkeypatch.setattr(sec, "_parse_request", lambda _r: (False, LONG_IPV6))
    monkeypatch.setattr(sec, "presented_token", lambda: "")
    monkeypatch.setattr(sec, "enforce_unlock_token", lambda: True)
    monkeypatch.setattr(sec.lock_state, "record_locked", lambda *_a, **_k: None)
    out = {}
    for name, state, sess in (("session", live, "sess-x"), ("no_session", live, ""),
                              ("not_unlocked", {}, "")):
        monkeypatch.setattr(sec, "_load_state", lambda s=state: s)
        monkeypatch.setattr(sec, "_current_session_fingerprint", lambda v=sess: v)
        monkeypatch.setattr(sec, "session_auth_enabled", lambda: True)
        out[name] = sec.require_unlocked()
    return out


@pytest.mark.parametrize("name", ["session", "no_session", "not_unlocked"])
def test_every_lock_refusal_names_the_fleet_submit_call_to_make_now(monkeypatch, name):
    msg = _refusals(monkeypatch)[name]
    assert msg and msg.startswith("[locked"), msg
    assert "fleet_submit(goal=" in msg and "source=" in msg
    assert "do not ask the user to choose" in msg
    assert "QUEUED" in msg
    assert "unlock(password=" in msg  # holders of the password keep their instruction
    assert "do not stop" not in msg.lower()
    # Must stay a whole short tool return, or the relay stops recognising it as a lock.
    assert len(msg) < DOMINANCE_MAX, (len(msg), msg)
