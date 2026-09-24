"""The bridge's unlock budget is per conversation, not per bridge process.

With the unlock second factor on by default (e25b7a3) an unlock is bound to the MCP session
that made it, and every new Copilot conversation is a new MCP session. The bridge injected the
proactive unlock once per PROCESS and at most MAX_BRIDGE_UNLOCK_ATTEMPTS (3) per process
lifetime, so a bridge that stayed up served three conversations and then left every later one
locked -- the agent asking a human for a password that is in .env.

Run for real: the actual Handler._run_one_turn with the browser layer (_send_and_stream_once)
stubbed, a throwaway session store, and tools.lock_state redirected to tmp so a "refused for
lock" record is written exactly as the server writes it.

Run: pytest -q bridge/test_bridge_unlock_budget_per_conversation.py
"""
import tempfile
import time
from pathlib import Path

import pytest

PW = "pw-for-tests"


@pytest.fixture()
def rig(monkeypatch, tmp_path):
    import importlib

    import bridge.session_store as S
    monkeypatch.setenv(S.STORE_DIR_ENV, tempfile.mkdtemp())
    importlib.reload(S)

    import bridge.copilot_bridge as B
    import tools.lock_state as LS
    monkeypatch.setattr(B, "S", S, raising=False)
    monkeypatch.setattr(LS, "_LOG_FILE", Path(str(tmp_path / "refusals.jsonl")))
    monkeypatch.setattr(LS, "_STATE_FILE", Path(str(tmp_path / "state.json")))
    monkeypatch.setattr(B, "_BRIDGE_UNLOCK_BY_CONV", {})
    monkeypatch.setattr(B, "_BRIDGE_UNLOCK_TIMES", [])
    monkeypatch.setattr(B, "_prepare_capture_baseline", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(B, "_bridge_unlock_password", lambda *a, **k: PW, raising=False)
    monkeypatch.setattr(B, "_bridge_recycle_if_exhausted", lambda final: False, raising=False)

    class _H(object):
        """The browser layer, stubbed: records what was sent; `locked` makes every turn that
        does not carry the unlock prefix leave a real lock refusal, as the server would."""
        locked = False

        def __init__(self):
            self.sent = []

        def _send_and_stream_once(self, payload, stream_out=True):
            self.sent.append(payload)
            if self.locked and not payload.startswith(B.BRIDGE_UNLOCK_PREFIX[:10]):
                LS.record_locked("203.0.113.7", "[locked client IP: '203.0.113.7'] call unlock",
                                 ts=time.time())
            return "answer"

    h = _H()
    h._run_one_turn = B.Handler._run_one_turn.__get__(h, _H)

    def new_conversation(title="c"):
        sid = S.new_session(title)["sid"]
        monkeypatch.setattr(B, "ACTIVE_SID", sid, raising=False)
        return sid

    return B, h, new_conversation


def _unlocks(B, sent):
    return [p for p in sent if p.startswith(B.BRIDGE_UNLOCK_PREFIX[:10])]


def test_every_new_conversation_gets_its_own_proactive_unlock(rig):
    """The defect itself: conversation 4 onwards used to get no unlock at all."""
    B, h, new_conversation = rig
    n = B.MAX_BRIDGE_UNLOCK_ATTEMPTS + 5
    for i in range(n):
        sid = new_conversation("c%d" % i)
        before = len(h.sent)
        h._run_one_turn(sid, "task %d" % i, stream_out=False)
        first = h.sent[before]
        assert first.startswith(B.BRIDGE_UNLOCK_PREFIX[:10]), "conversation %d got no unlock" % i
        assert first.endswith("task %d" % i)
    assert len(_unlocks(B, h.sent)) == n


def test_later_turns_of_one_conversation_are_not_prefixed(rig):
    B, h, new_conversation = rig
    sid = new_conversation()
    h._run_one_turn(sid, "one", stream_out=False)
    h._run_one_turn(sid, "two", stream_out=False)
    h._run_one_turn(sid, "three", stream_out=False)
    assert h.sent[1:] == ["two", "three"], h.sent
    assert len(_unlocks(B, h.sent)) == 1


def test_an_unlock_loop_inside_one_conversation_is_still_capped(rig):
    """Every turn refused, every unlock retry refused again: bounded per conversation."""
    B, h, new_conversation = rig
    type(h).locked = True
    try:
        # Unlocks never take here: make every send (prefixed or not) leave a refusal.
        import tools.lock_state as LS
        orig = type(h)._send_and_stream_once

        def always_refused(self, payload, stream_out=True):
            self.sent.append(payload)
            LS.record_locked("203.0.113.7", "[locked client IP: '203.0.113.7'] x", ts=time.time())
            return "answer"
        type(h)._send_and_stream_once = always_refused
        sid = new_conversation()
        for i in range(10):
            h._run_one_turn(sid, "t%d" % i, stream_out=False)
        assert len(_unlocks(B, h.sent)) == B.MAX_BRIDGE_UNLOCK_ATTEMPTS, h.sent
        # ... and the NEXT conversation starts with a full budget again.
        before = len(_unlocks(B, h.sent))
        sid2 = new_conversation("next")
        h._run_one_turn(sid2, "fresh", stream_out=False)
        assert len(_unlocks(B, h.sent)) - before == 2   # proactive + one reactive retry
    finally:
        type(h)._send_and_stream_once = orig
        type(h).locked = False


def test_a_refused_turn_is_retried_with_the_unlock(rig):
    """The reactive path per conversation: a turn refused for lock is redone with the prefix."""
    B, h, new_conversation = rig
    sid = new_conversation()
    h._run_one_turn(sid, "first", stream_out=False)      # proactive
    type(h).locked = True
    try:
        h._run_one_turn(sid, "second", stream_out=False)
    finally:
        type(h).locked = False
    assert h.sent[-2] == "second" and h.sent[-1].endswith("second")
    assert h.sent[-1].startswith(B.BRIDGE_UNLOCK_PREFIX[:10])


def test_a_runaway_across_conversations_hits_the_window(rig, monkeypatch):
    B, h, new_conversation = rig
    monkeypatch.setattr(B, "MAX_BRIDGE_UNLOCKS_PER_WINDOW", 4)
    now = [1000.0]
    monkeypatch.setattr(B.time, "time", lambda: now[0])
    for i in range(7):
        h._run_one_turn(new_conversation("c%d" % i), "t%d" % i, stream_out=False)
    assert len(_unlocks(B, h.sent)) == 4
    now[0] += B.BRIDGE_UNLOCK_WINDOW_S + 1
    h._run_one_turn(new_conversation("later"), "later", stream_out=False)
    assert h.sent[-1].startswith(B.BRIDGE_UNLOCK_PREFIX[:10]), "the window never reopened"


def test_a_recycled_fresh_chat_gets_its_own_unlock(rig, monkeypatch):
    """Out-of-budget recycle opens a new chat = a new MCP session. The resend must carry an
    unlock for it, built from the message (never a doubled prefix)."""
    B, h, new_conversation = rig
    sid = new_conversation()
    h._run_one_turn(sid, "warm-up", stream_out=False)     # the old chat's unlock is spent
    fired = []

    def recycle(final):
        if fired:
            return False
        fired.append(1)
        new_conversation("fresh")
        return True
    monkeypatch.setattr(B, "_bridge_recycle_if_exhausted", recycle)
    monkeypatch.setattr(B, "ACTIVE_SID", sid, raising=False)
    h._run_one_turn(sid, "long task", stream_out=False)
    assert h.sent[-2] == "long task"                       # old chat: no second proactive
    resend = h.sent[-1]
    assert resend.startswith(B.BRIDGE_UNLOCK_PREFIX[:10]) and resend.endswith("long task")
    assert resend.count(B.BRIDGE_UNLOCK_PREFIX[:10]) == 1


def test_a_turn_refused_for_the_wrong_session_does_not_spend_the_preflight(rig):
    B, h, new_conversation = rig
    a = new_conversation("A")
    import bridge.session_store as S
    b = S.new_session("B")["sid"]
    out = h._run_one_turn(b, "misaddressed", stream_out=False)
    assert isinstance(out, dict) and out.get("wrong_session")
    h._run_one_turn(a, "real", stream_out=False)
    assert h.sent == [(B.BRIDGE_UNLOCK_PREFIX % PW) + "real"]
