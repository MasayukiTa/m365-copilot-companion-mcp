"""The route breaker counts CONNECTION/TOKEN failures, never one conversation's stall.

2026-10-15 15:28: three brand-new conversations received update frames but no content for 60 s,
three "consecutive failures" closed the route for the whole fleet while other workers were
progressing normally, and every goal was held. These tests replay that with fake drivers.
"""
import json
from types import SimpleNamespace

import pytest

from relay import relay_fleet as rf
from relay.chathub import frame_shape
from relay.route_reopen import ReopenPolicy
from relay.socket_driver import CopilotSocketDriver
from relay.socket_route import SocketRoute


class _Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class _StreamDrv:
    """A live socket turn: `idle` s since meaningful progress, `frame_idle` s since any frame."""

    def __init__(self, idle, frame_idle):
        self.idle, self.frame_idle = idle, frame_idle
        self.failed, self.stalled, self.closed = "", False, False

    def _is_generating(self):
        return True

    def generation_idle_s(self):
        return self.idle

    def frame_idle_s(self):
        return self.frame_idle

    def fail_stalled_turn(self, reason):
        self.failed = reason + " [frames: 1/update x68]"
        self.stalled = True

    def partial_text(self):
        return ""

    def _answers(self):
        return SimpleNamespace(count=lambda: 0)

    def close(self):
        self.closed = True


def _route(tmp_path, clock, **kw):
    return SocketRoute(enabled=True, connect_fn=object(), now=clock,
                       log_path=str(tmp_path / "route.jsonl"), **kw)


def _events(tmp_path, name=None):
    p = tmp_path / "route.jsonl"
    if not p.exists():
        return []
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r for r in rows if name is None or r["event"] == name]


def _worker(name, drv):
    w = rf.RelayWorker("do the thing", name)
    w.socket, w.drv, w.status = True, drv, "waiting"
    w.turn, w._last_sent_job = 1, "do the thing"
    return w


@pytest.fixture(autouse=True)
def _no_real_log(tmp_path, monkeypatch):
    from relay import socket_route as SR
    monkeypatch.setattr(SR, "DEFAULT_LOG", str(tmp_path / "isolated.jsonl"))


# ---- the 15:28 scenario ------------------------------------------------------------------------

def test_three_stalled_conversations_do_not_close_the_route_while_others_progress(
        tmp_path, monkeypatch):
    clock = _Clock()
    route = _route(tmp_path, clock)
    monkeypatch.setattr(rf, "_socket_route", lambda: route)
    stalled = [_worker("w%d" % i, _StreamDrv(idle=400, frame_idle=1)) for i in (4, 5, 8)]
    healthy = [_worker("w%d" % i, _StreamDrv(idle=2, frame_idle=1)) for i in (0, 6, 3)]

    clock.t += 200                                   # a long silent stretch for the stalled ones
    for sweep in range(3):
        for w in healthy + stalled:                  # every sweep polls the healthy ones too
            w._socket_meaningful_idle_stalled(now=clock.t)
        for w in stalled:
            w.poll()
        clock.t += 1

    assert route.open(), route.closed_reason
    for w in stalled:
        assert w.drv is None and w._needs_socket_rebind and w.status == "ready"
        assert w._stalled_recycles == 1             # exactly one fresh-conversation recycle
    for w in healthy:
        assert w.drv is not None and not w.drv.failed
    assert len(_events(tmp_path, "conversation_stall")) == 3
    assert not _events(tmp_path, "route_closed")


@pytest.mark.parametrize("reason", [
    "w1: ChatHubError: could not open the socket: OSError: refused",
    "w1: HTTP 401 unauthorized",
    "w1: ConnectionClosedError: no close frame received or sent",
    "w1: gaierror: getaddrinfo failed (name resolution)",
])
def test_three_genuine_connection_errors_close_the_route(tmp_path, reason):
    clock = _Clock()
    route = _route(tmp_path, clock)
    for i in range(3):
        route.note_failure(reason.replace("w1", "w%d" % i))
    assert not route.open()
    trips = _events(tmp_path, "route_breaker_trip")
    assert len(trips) == 1 and trips[0]["kind"] == "connection"
    assert trips[0]["per_worker"] == {"w0": 1, "w1": 1, "w2": 1}
    assert _events(tmp_path, "route_closed")


def test_progress_on_another_conversation_breaks_the_consecutive_run(tmp_path):
    clock = _Clock()
    route = _route(tmp_path, clock)
    route.note_failure("w0: could not open the socket: x")
    route.note_failure("w1: could not open the socket: x")
    route.note_progress("w6")                        # another conversation is alive
    route.note_failure("w2: could not open the socket: x")
    assert route.open() and route.consecutive == 1


def test_stalls_close_the_route_only_when_nothing_progresses_anywhere(tmp_path):
    clock = _Clock()
    route = _route(tmp_path, clock)
    clock.t += 300
    frames = "1/update x68"
    for i in range(3):
        route.note_stall("w%d" % i, "no meaningful progress for 70s [frames: %s]" % frames)
    assert not route.open()
    trip = _events(tmp_path, "route_breaker_trip")[0]
    assert trip["kind"] == "stalls_with_no_progress" and trip["frames"] == {"1/update": 204}


def test_a_stall_reason_is_never_counted_as_a_connection_failure(tmp_path):
    route = _route(tmp_path, _Clock())
    for i in range(5):
        route.note_failure("w%d: socket turn made no meaningful progress for 61s (limit 60s)"
                           " [frames: 1/update x68]" % i)
    assert route.open() and route.consecutive == 0 and route.fallbacks == 0


# ---- idle limit: a live stream gets the longer limit -------------------------------------------

@pytest.mark.parametrize("idle,frame_idle,stalled", [
    (100.0, 1.0, False),      # frames keep arriving: alive
    (100.0, None, True),      # no frames at all: short limit
    (100.0, 90.0, True),      # frames stopped too: silent
    (350.0, 1.0, True),       # even a live stream has a ceiling
    (30.0, None, False),
])
def test_idle_limit_depends_on_whether_the_stream_is_alive(tmp_path, monkeypatch,
                                                           idle, frame_idle, stalled):
    route = _route(tmp_path, _Clock())
    monkeypatch.setattr(rf, "_socket_route", lambda: route)
    w = _worker("w0", _StreamDrv(idle, frame_idle))
    assert w._socket_meaningful_idle_stalled(now=0.0) is stalled


# ---- reopen cadence ----------------------------------------------------------------------------

def test_the_first_reopen_probe_is_immediate_and_retries_stay_short(tmp_path):
    clock = _Clock()
    route = _route(tmp_path, clock)
    route.close_route("3 consecutive failures, last: w0: could not open the socket: x")
    ok = [False]
    p = ReopenPolicy(now=clock, log=lambda m: None, probe=lambda r: ok[0], spawn=lambda f: f())
    p.consider(route)                                # sees the close
    p.consider(route)                                # probe runs at once, same instant
    p.consider(route)                                # reads the (failed) answer
    assert p.probes == 1 and p.wait_s <= 20
    for _ in range(6):
        clock.t += 21
        p.consider(route); p.consider(route)
    assert p.wait_s <= 20
    ok[0] = True
    clock.t += 21
    p.consider(route)
    p.consider(route)
    assert p.consider(route) is True


# ---- per-turn stats ----------------------------------------------------------------------------

def test_frame_shape_keeps_key_names_only():
    frame = {"type": 1, "target": "update",
             "arguments": [{"messages": [{"messageType": "InternalSearchQuery",
                                          "text": "SECRET-VALUE", "author": "bot"}]}]}
    shape = frame_shape(frame)
    assert "SECRET-VALUE" not in json.dumps(shape)
    assert shape["messages"][0]["messageType"] == "InternalSearchQuery"
    assert shape["messages"][0]["text_empty"] is False


def test_turn_stats_are_emitted_with_timings_and_frame_counts():
    conv = SimpleNamespace(turn_t0=0.0, first_frame_s=1.5, first_content_s=None,
                           frame_kinds={"1/update": 68}, empty_update_shapes=[{"keys": ["type"]}])
    drv = CopilotSocketDriver(conv, connect=None)
    got = []
    drv.stats_sink, drv.worker_name = got.append, "w4"
    drv._emit_turn_stats("stalled", "x")
    s = got[0]
    assert s["worker"] == "w4" and s["outcome"] == "stalled" and s["first_frame_s"] == 1.5
    assert s["first_content_s"] is None and s["frames"] == {"1/update": 68}
