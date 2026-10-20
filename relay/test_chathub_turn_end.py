"""One `turn_end` row per exchange, so the sliding deadline can be judged from data.

MEASURED 2026-10-20: no record showed the sliding deadline (PR #198) working in production, and
the cut events carried too little data to tell a productive turn from a silent one. These tests
use a fake socket and an injectable clock: nothing sleeps.
"""
import json

import pytest

from relay import chathub as CH
from relay import socket_driver as SD
from relay.test_chathub_sliding_deadline import (
    TIMEOUT, _Clock, _TickSock, _done, _empty_update, _token, _update)


def _conv(frames, step=10.0, **kw):
    clock = _Clock()
    rows = []
    conv = CH.Conversation(lambda: _token(), turn_timeout_s=TIMEOUT, clock=clock, **kw)
    conv.turn_end_sink = rows.append
    sock = _TickSock(clock, step, frames)
    return conv, clock, rows, lambda **k: conv.ask("x", connect=lambda u, h, t: sock, **k)


def test_progress_extends_and_the_turn_ends_normally_with_one_row():
    conv, clock, rows, go = _conv(lambda i: _done() if i > 25 else _update("w"))
    go()
    assert len(rows) == 1
    r = rows[0]
    assert r["outcome"] == "completed" and r["ended_by"] == "completion"
    assert r["extensions"] > 0
    assert r["duration_s"] == pytest.approx(260.0, abs=10.0)
    assert r["seconds_since_progress"] <= 20.0
    assert r["progress_seen"] is True
    assert r["seconds_since_tool"] is None and r["tool_calls"] == 0
    assert conv.last_turn_end == r


def test_a_silent_turn_records_seconds_since_progress_at_least_the_timeout():
    conv, clock, rows, go = _conv(lambda i: _empty_update())
    with pytest.raises(CH.ChatHubError):
        go()
    assert len(rows) == 1
    r = rows[0]
    assert r["outcome"] == "deadline_exceeded" and r["ended_by"] == "timeout"
    assert r["extensions"] == 0 and r["progress_seen"] is False
    assert r["seconds_since_progress"] >= TIMEOUT


def test_progress_then_silence_counts_the_extensions_and_the_gap():
    conv, clock, rows, go = _conv(lambda i: _update("a") if i <= 3 else _empty_update())
    with pytest.raises(CH.ChatHubError):
        go()
    r = rows[0]
    assert r["outcome"] == "deadline_exceeded" and r["extensions"] >= 1
    assert r["seconds_since_progress"] >= TIMEOUT


def test_the_ceiling_is_recorded_as_the_ender():
    conv, clock, rows, go = _conv(lambda i: _update("w"))
    with pytest.raises(CH.ChatHubError) as exc:
        go()
    assert "turn ceiling exceeded" in str(exc.value)
    assert len(rows) == 1
    r = rows[0]
    assert r["outcome"] == "ceiling_exceeded" and r["ended_by"] == "ceiling"
    assert r["extensions"] > 0
    assert r["seconds_since_progress"] < TIMEOUT
    assert r["duration_s"] == pytest.approx(3 * TIMEOUT, abs=10.0)


def test_other_failures_are_outcome_error():
    conv, clock, rows, go = _conv(lambda i: None)
    with pytest.raises(CH.ChatHubError):
        go()
    assert rows[0]["outcome"] == "error" and rows[0]["ended_by"] == "error"


def test_tool_calls_are_counted_and_timed_across_rounds(monkeypatch):
    from relay import socket_tools
    clock = _Clock()
    rows = []
    conv = CH.Conversation(lambda: _token(), turn_timeout_s=TIMEOUT, clock=clock)
    conv.turn_end_sink = rows.append
    state = {"n": 0}

    def connect(u, h, t):
        return _TickSock(clock, 10.0, lambda i: _done() if i == 2 else _update("w"))

    def run_tool(name, args):
        clock.t += 5.0
        return True, "ok"

    def step(answer, run):
        if state["n"] == 0:
            state["n"] = 1
            run("t", {})
            run("t", {})
            return "next", [("t", {}), ("t", {})]
        return None, []

    monkeypatch.setattr(socket_tools, "step", step)
    conv.ask("x", connect=connect, run_tool=run_tool)
    assert len(rows) == 2
    assert rows[0]["tool_calls"] == 0
    assert rows[1]["tool_calls"] == 2
    assert 0.0 <= rows[1]["seconds_since_tool"] < 30.0
    assert rows[1]["exchange"] == 1


def test_a_failing_sink_never_costs_the_turn():
    conv, clock, rows, go = _conv(lambda i: _done() if i > 2 else _update("w"))

    def boom(row):
        raise RuntimeError("disk full")
    conv.turn_end_sink = boom
    assert go() == "ww"
    assert conv.last_turn_end["outcome"] == "completed"


def test_the_row_is_small_and_carries_the_conversation_only_as_a_hash():
    conv, clock, rows, go = _conv(lambda i: _done() if i > 2 else _update("secret words"))
    go()
    r = rows[0]
    text = json.dumps(r)
    assert "secret" not in text and conv.conversation_id not in text
    assert len(r["conv"]) == 12 and len(text) < 600


def test_the_driver_wires_the_conversation_to_the_routes_record():
    class Route:
        def __init__(self):
            self.rows = []

        def record(self, event, **f):
            self.rows.append((event, f))

        def stats(self, s):
            pass

    class Conv:
        turn_end_sink = None

    route = Route()
    drv = SD.CopilotSocketDriver.__new__(SD.CopilotSocketDriver)
    drv.conv = Conv()
    drv.stats_sink = route.stats
    drv.worker_name = "w1"
    drv._turn_seq = 4
    drv._wire_turn_end()
    drv.conv.turn_end_sink({"outcome": "completed", "extensions": 2})
    assert route.rows == [("turn_end", {"worker": "w1", "turn": 4,
                                        "outcome": "completed", "extensions": 2})]


def test_the_driver_without_a_route_writes_nothing_and_does_not_raise():
    class Conv:
        turn_end_sink = None
    drv = SD.CopilotSocketDriver.__new__(SD.CopilotSocketDriver)
    drv.conv = Conv()
    drv._wire_turn_end()
    assert drv.conv.turn_end_sink is None
