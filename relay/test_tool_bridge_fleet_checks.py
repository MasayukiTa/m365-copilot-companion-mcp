# -*- coding: utf-8 -*-
"""Five fleet checks assume the agent holds an MCP connector. A bridged worker (its driver has
a run_tool) has none by design, so for it -- and only for it -- each check is skipped or relaxed.

For every check there are two halves, and the first is the one that matters most: a CONNECTOR
worker is judged exactly as before. A bridge test with no connector twin would pass for a check
that was simply deleted.

  A  no MCP `initialize` within the window  -> recycle the conversation
  B  the model says it has no call_tool     -> recycle the conversation
  D  a call written out as text             -> steer, then recycle, then INFRA_STUCK
     (a bridged worker is not skipped any more: it is judged by relay/bridged_reply_checks.py)
  F  no meaningful progress for 60 s        -> fail the turn, recycle
  G  no reply within the outer 240 s clock  -> retry / STUCK
"""
from __future__ import annotations

import time
import types

import pytest

import relay.relay_fleet as F
from relay import mcp_ready
from relay import settle as S
from relay import tool_bridge as TB

GOAL = "テスト用のゴール"
MALFORMED = ('call\n<invoke name="x-call_tool">\n<parameter name="arguments">{}</parameter>\n'
             '<parameter name="name">call_tool</parameter>\n</invoke>')
NO_CALL_TOOL = "私の関数一覧に call_tool はありません。"


def _bridged_drv(**kw):
    return types.SimpleNamespace(_run_tool=lambda n, a: (True, ""), **kw)


@pytest.fixture()
def worker(monkeypatch):
    monkeypatch.setattr(F, "_unlock_password", lambda: "pw-placeholder-not-a-credential")
    monkeypatch.setattr(F, "connector_proof_source", lambda: "run")
    w = F.RelayWorker(GOAL, "w0")
    w._last_sent_job = "FIRST MESSAGE"
    w.socket = True
    return w


def _bridge(w):
    w.drv = _bridged_drv()
    assert TB.worker_is_bridged(w)
    return w


def _connector(w):
    w.drv = types.SimpleNamespace(_run_tool=None)
    assert not TB.worker_is_bridged(w)
    return w


def _decide(w, resp, turn):
    w.turn = turn
    w.status = "waiting"
    w._decide(resp)


# ------------------------------------------------------------------------------------------ A

class _Det:
    def __init__(self, status="untracked"):
        self._status = status

    def status(self, name):
        return self._status


def _recycle_spy(w):
    calls = []
    w._recycle_for_missing_tools = lambda why: calls.append(why)
    return calls


def test_A_connector_worker_is_recycled_when_no_initialize_arrives(worker, monkeypatch):
    monkeypatch.setattr(mcp_ready, "decide_tools", lambda *a, **k: "recycle")
    calls = _recycle_spy(_connector(worker))
    worker._mcp_tracking = True
    worker._check_tools_attached()
    assert len(calls) == 1 and "no MCP initialize" in calls[0]


def test_A_bridged_worker_is_never_recycled_for_a_missing_initialize(worker, monkeypatch):
    monkeypatch.setattr(mcp_ready, "decide_tools",
                        lambda *a, **k: pytest.fail("a bridged worker must not be judged"))
    calls = _recycle_spy(_bridge(worker))
    worker._mcp_tracking = True
    worker._check_tools_attached()
    assert calls == [] and worker._mcp_tracking is False


# ------------------------------------------------------------------------------------------ B

def test_B_connector_worker_is_recycled_when_the_model_says_it_has_no_call_tool(worker, monkeypatch):
    monkeypatch.setattr(mcp_ready, "detector", lambda: _Det("untracked"))
    calls = _recycle_spy(_connector(worker))
    _decide(worker, NO_CALL_TOOL, 3)
    assert calls == ["the model reports it has no call_tool"]


def test_B_bridged_worker_is_not_recycled_for_saying_it_has_no_call_tool(worker, monkeypatch):
    monkeypatch.setattr(mcp_ready, "detector", lambda: _Det("untracked"))
    calls = _recycle_spy(_bridge(worker))
    _decide(worker, NO_CALL_TOOL, 3)
    assert calls == []


# ------------------------------------------------------------------------------------------ D

def test_D_the_detector_itself_is_unchanged_for_the_default_call():
    assert F._tried_to_call_a_tool(MALFORMED) is True
    assert F._tried_to_call_a_tool("call_tool(name='x')") is True
    assert F._tried_to_call_a_tool(MALFORMED, False) is True
    assert F._tried_to_call_a_tool("普通の返信") is False


def test_D_the_detector_does_not_apply_to_a_bridged_worker():
    assert F._tried_to_call_a_tool(MALFORMED, True) is False
    assert F._tried_to_call_a_tool("call_tool(name='x')", True) is False


def test_D_connector_worker_is_steered_then_recycled(worker, monkeypatch):
    monkeypatch.setattr(F.time, "time", lambda: 1000.0)
    _connector(worker)
    _decide(worker, MALFORMED, 1)
    assert worker._unlanded_calls == 1 and "ツール呼び出しの仕組み" in worker.job
    _decide(worker, MALFORMED, 2)
    assert worker._malformed_recycles == 1


def test_D_bridged_worker_never_gets_the_connector_steer(worker, monkeypatch):
    """The connector's steer tells the model to use "the tool-calling mechanism", which on the
    connector-less route IS the code block. A bridged worker is judged by
    relay/bridged_reply_checks.py instead (tests in relay/test_bridged_reply_checks.py), and its
    correction shows the block format."""
    monkeypatch.setattr(F.time, "time", lambda: 1000.0)
    _bridge(worker)
    _decide(worker, MALFORMED + "\nstep 1", 1)
    assert "ツール呼び出しの仕組み" not in worker.job
    assert "```call_tool" in worker.job


def test_D_bridged_prose_about_a_call_that_was_made_is_not_a_fault(worker, monkeypatch):
    """A call spelled `call_tool(` in the summary of a turn that DID execute calls is normal."""
    monkeypatch.setattr(F.time, "time", lambda: 1000.0)
    _bridge(worker)
    worker.drv.conv = types.SimpleNamespace(_tool_calls=2)
    for turn in (1, 2, 3):
        _decide(worker, "call_tool(name='x') で確認しました。続けます。step %d" % turn, turn)
    assert getattr(worker, "_unlanded_calls", 0) == 0
    assert worker._malformed_recycles == 0
    assert worker.outcome != "INFRA_STUCK" and worker.status != "stuck"


# ------------------------------------------------------------------------------------------ F

class _GenDrv:
    def __init__(self, idle_s, frame_idle_s=None, run_tool=False):
        self.idle_s, self.frame = idle_s, frame_idle_s
        self.failed = ""
        if run_tool:
            self._run_tool = lambda n, a: (True, "")

    def _is_generating(self):
        return True

    def generation_idle_s(self):
        return self.idle_s

    def frame_idle_s(self):
        return self.frame

    def fail_stalled_turn(self, reason):
        self.failed = reason


class _Route:
    def record(self, *a, **k):
        pass

    def note_progress(self, *a, **k):
        pass


def _idle_worker(idle_s, *, bridged, frame_idle_s=None):
    w = F.RelayWorker("g", "w0")
    w.socket = True
    w.drv = _GenDrv(idle_s, frame_idle_s, run_tool=bridged)
    w.status = "waiting"
    return w


@pytest.fixture()
def idle_limits(monkeypatch):
    monkeypatch.setattr(F, "_socket_route", lambda: _Route())
    monkeypatch.setattr(F, "SOCKET_MEANINGFUL_IDLE_S", 60.0)
    monkeypatch.setattr(F, "SOCKET_LIVE_STREAM_IDLE_S", 300.0)
    monkeypatch.setenv("MCP_FLEET_BRIDGE_IDLE_S", "900")


def test_F_connector_silence_over_the_limit_fails_the_turn(idle_limits):
    w = _idle_worker(61.0, bridged=False)
    assert w._socket_meaningful_idle_stalled() is True
    assert "no meaningful progress" in w.drv.failed and "limit 60s" in w.drv.failed


def test_F_connector_live_stream_gets_the_longer_limit_as_before(idle_limits):
    assert _idle_worker(200.0, bridged=False, frame_idle_s=5.0)._socket_meaningful_idle_stalled() is False
    assert _idle_worker(301.0, bridged=False, frame_idle_s=5.0)._socket_meaningful_idle_stalled() is True


def test_F_bridged_silence_that_would_fail_a_connector_turn_is_tolerated(idle_limits):
    w = _idle_worker(61.0, bridged=True)
    assert w._socket_meaningful_idle_stalled() is False
    assert w.drv.failed == "" and w.status == "waiting"
    w = _idle_worker(600.0, bridged=True, frame_idle_s=None)
    assert w._socket_meaningful_idle_stalled() is False


def test_F_bridged_silence_still_fails_at_the_longer_limit(idle_limits):
    w = _idle_worker(901.0, bridged=True)
    assert w._socket_meaningful_idle_stalled() is True
    assert "limit 900s" in w.drv.failed


# ------------------------------------------------------------------------------------------ G

class _Answers:
    def count(self):
        return 0


class _Driver:
    def __init__(self, run_tool):
        if run_tool:
            self._run_tool = lambda n, a: (True, "")

    def _answers(self):
        return _Answers()

    def _is_generating(self):
        return False

    def read_last_response(self):
        return ""

    def _is_stale_repeat(self, _t):
        return False


class _ClockWorker:
    """The same minimal stand-in test_reply_beats_timeout uses, around the real poll()."""
    poll = F.RelayWorker.poll
    status = "waiting"
    per_turn_timeout_s = 240
    dwell_s = 4.0
    _count_before = 0
    transient = 0
    max_transient = 10
    last_response = ""

    def __init__(self, *, bridged, sent_ago):
        self.drv = _Driver(bridged)
        self._t_send = time.time() - sent_ago
        self._last_text = None
        self._stable_since = None
        self._settle_state = S.SettleState()
        self.retried = False
        self.outcome = None
        self.reason = ""
        self.notes = []

    def _capture_url(self):
        pass

    def _retry_transient(self):
        self.retried = True
        self.status = "ready"
        return True

    def _note_timeout(self, *a, **k):
        self.notes.append(k.get("budget_s"))

    def _salvage_via_checks(self):
        return False

    def _decide(self, text):
        self.status = "done"


def test_G_connector_turn_past_240_seconds_is_retried(monkeypatch):
    monkeypatch.setenv("MCP_SETTLE_UNIFIED", "0")
    w = _ClockWorker(bridged=False, sent_ago=300)
    assert w.poll() is False
    assert w.retried is True and w.notes == [240]


def test_G_bridged_turn_past_240_seconds_keeps_waiting(monkeypatch):
    monkeypatch.setenv("MCP_SETTLE_UNIFIED", "0")
    w = _ClockWorker(bridged=True, sent_ago=300)
    assert w.poll() is False
    assert w.retried is False and w.status == "waiting"


def test_G_bridged_turn_past_the_longer_clock_is_still_cut_off(monkeypatch):
    monkeypatch.setenv("MCP_SETTLE_UNIFIED", "0")
    monkeypatch.setenv("MCP_FLEET_BRIDGE_TURN_S", "1000")
    w = _ClockWorker(bridged=True, sent_ago=1100)
    assert w.poll() is False
    assert w.retried is True and w.notes == [1000.0]
