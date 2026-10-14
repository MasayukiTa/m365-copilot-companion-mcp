"""The no-turn token capture must keep a token fresh for a fleet whose goals are all admitted.

Root cause (production 2026-10-14 18:52 -> 5+ hours, goals w0/w1 "waiting for token", status.json
`token_wait` {}): the capture was kicked only from inside `while pending:` in the sweep, i.e. only
while the ADMISSION QUEUE was non-empty. A route reopen builds a route with no token; with every
goal already admitted nobody captured, and the workers that had lost their socket waited forever.
These tests pin the pieces of the repair:

  * the kick runs every lap while a goal is unfinished, outside the admission loop;
  * a reopen carries a still-live credential over;
  * the retry backoff is bounded (30, 60, 120 s, never longer while a goal is held);
  * the capture's state is visible in status.json `token_wait`, for rebinding workers too;
  * a text-less tool-status frame counts as progress, and a stall says what the stream did.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest

from relay import chathub as CH
from relay.token_provider import CapturePolicy, TokenHold, capture_state, status_block


def _tok(exp):
    body = json.dumps({"oid": "o", "tid": "t", "exp": exp}).encode()
    return "x." + base64.urlsafe_b64encode(body).decode().rstrip("=") + ".y"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    import relay.capture_status as cs
    monkeypatch.setattr(cs, "record_success", lambda *_a, **_k: None)
    monkeypatch.setattr(cs, "record_failure", lambda *_a, **_k: None)
    capture_state().reset()
    import relay.token_provider as tp
    monkeypatch.setattr(tp, "_HOLD", None)
    yield
    capture_state().reset()


# ---- the kick is not tied to the admission queue --------------------------------------------

def _fleet_src():
    return Path("relay/relay_fleet.py").read_text(encoding="utf-8")


def test_capture_kick_runs_before_and_outside_the_admission_loop():
    src = _fleet_src()
    start = src.index("def run_relay_fleet(")
    end = src.index("# IS THE ROUTE STILL RIGHT TO BE SHUT?", start)
    body = src[start:end]
    loop = body.index("        while pending:")
    kick = body.index("_consider_socket_refresh(route, agent_url)")
    assert kick < loop, "the capture kick must not live inside `while pending:`"
    # and the loop body no longer carries a second copy of it
    assert "_consider_socket_refresh(route, agent_url)" not in body[loop:]
    # the guard is "any goal unfinished", not "the queue is non-empty"
    guard = body[kick - 400:kick]
    assert "_w.status not in TERMINAL for _w in workers" in guard


def test_rebinding_worker_registers_as_held_and_is_released():
    src = _fleet_src()
    i = src.index("def _rebind_socket(self):")
    block = src[i:i + 2600]
    assert "token_provider.hold().hold_reason(self.name, route, self._agent_url)" in block
    assert "token_provider.hold().release(self.name)" in block


def test_reopen_carries_a_live_credential_over():
    from relay.chathub import RequestTemplate
    from relay.relay_fleet import _live_route_entries
    from relay.socket_route import SocketRoute

    tpl = RequestTemplate({"gptId": "x"}, {"threadLevelGptId": {"id": "T_agent.x"}})
    r = SocketRoute(enabled=True, capture_fn=None, connect_fn=object(), log_path="")
    live, dead = _tok(time.time() + 3000), _tok(time.time() - 5)
    assert r.install_capture(live, tpl, "A")
    assert r.install_capture(dead, tpl, "B")
    got = _live_route_entries(r)
    assert set(got) == {"A"}
    assert got["A"]["token"] == live
    fresh = SocketRoute(enabled=True, capture_fn=None, connect_fn=object(), log_path="")
    assert not fresh.ready("A")
    for k, e in got.items():
        assert fresh.install_capture(e["token"], e["template"], k)
    assert fresh.ready("A") and not fresh.ready("B")


# ---- bounded backoff -------------------------------------------------------------------------

def test_no_token_failure_backoff_is_30_60_120_and_never_longer():
    p = CapturePolicy(base_s=120, max_s=600, max_no_token_s=120, no_token_base_s=30,
                      now=lambda: 0.0)
    delays = [p.on_failure("a", have_token=False) for _ in range(8)]
    assert delays[:3] == [30, 60, 120]
    assert max(delays) == 120


def test_defaults_never_double_past_120_while_no_token_is_in_hand():
    import relay.token_provider as tp
    assert tp.MAX_INTERVAL_NO_TOKEN_S <= 120
    p = CapturePolicy(now=lambda: 0.0, no_token_base_s=tp.NO_TOKEN_BASE_S)
    assert max(p.on_failure("a", have_token=False) for _ in range(10)) <= 120


def test_a_held_goal_overrides_a_long_same_exp_interval():
    t = [1000.0]
    p = CapturePolicy(base_s=120, max_s=600, max_no_token_s=120, no_token_base_s=30,
                      now=lambda: t[0])
    tok = _tok(5000)
    for _ in range(4):
        d = p.on_success("a", tok)
    assert d == 600
    assert not p.allow("a", 1000 + 300)                  # idle fleet: the long interval stands
    assert not p.allow("a", 1000 + 119, held=True)
    assert p.allow("a", 1000 + 120, held=True)           # a held goal: never past the ceiling


def test_manager_retries_a_failed_capture_on_the_bounded_schedule_and_reports_it():
    from relay.chathub import RequestTemplate  # noqa: F401  (import path check)
    from relay.socket_capture_async import AsyncCaptureManager
    from relay.socket_route import SocketRoute

    class Bad:
        returncode = 1
        pid = 1

        def communicate(self, timeout=None):
            return b"", b"capture worker failed: NoTemplateWithoutTurn: no cached template"

    clock = [10000.0]
    mgr = AsyncCaptureManager(popen=lambda *a, **k: Bad(), interval_s=120, timeout_s=5,
                              now=lambda: clock[0], log=lambda _m: None)
    r = SocketRoute(enabled=True, capture_fn=None, connect_fn=object(), log_path="")

    def settle():
        deadline = time.time() + 2
        while time.time() < deadline and mgr.stats()["inflight"]:
            time.sleep(0.01)
        assert mgr.stats()["inflight"] == 0

    assert mgr.consider(r, "A") is True
    settle()
    snap = capture_state().snapshot()
    assert snap["attempts"] == 1 and snap["failures"] == 1
    assert "NoTemplateWithoutTurn" in snap["last_error"] or "exit 1" in snap["last_error"]
    assert 0 < snap["next_attempt_in_s"] <= 30
    clock[0] += 10
    assert mgr.consider(r, "A") is False                 # inside the 30 s backoff
    clock[0] += 25
    assert mgr.consider(r, "A") is True                  # 35 s after the failure: retried
    settle()
    assert capture_state().snapshot()["attempts"] == 2
    # the delay doubles to 60, then 120, and stops
    seen = []
    for _ in range(5):
        seen.append(capture_state().snapshot()["next_attempt_in_s"])
        clock[0] += 125
        assert mgr.consider(r, "A") is True
        settle()
    assert max(seen) <= 120 and max(capture_state().snapshot()["next_attempt_in_s"], 0) <= 120


def test_manager_with_no_goal_in_the_admission_queue_still_captures():
    """consider() has no notion of a queue: the sweep decides when to call it."""
    from relay.chathub import RequestTemplate
    from relay.socket_capture_async import AsyncCaptureManager
    from relay.socket_route import SocketRoute

    tpl = RequestTemplate({"gptId": "x"}, {"threadLevelGptId": {"id": "T_agent.x"}})
    payload = json.dumps({"token": _tok(time.time() + 3000), "query": tpl.query,
                          "frame": tpl.frame}).encode()

    class Good:
        returncode = 0
        pid = 1

        def communicate(self, timeout=None):
            return payload, b""

    mgr = AsyncCaptureManager(popen=lambda *a, **k: Good(), interval_s=120, timeout_s=5,
                              log=lambda _m: None)
    r = SocketRoute(enabled=True, capture_fn=None, connect_fn=object(), log_path="")
    assert mgr.consider(r, "A") is True
    deadline = time.time() + 2
    while time.time() < deadline and not r.ready("A"):
        time.sleep(0.01)
    assert r.ready("A")
    assert capture_state().snapshot()["successes"] == 1


# ---- status.json token_wait -------------------------------------------------------------------

class _Route:
    def __init__(self, open_=True, ready=False):
        self._o, self._r = open_, ready
        self.enabled, self.closed_reason = True, ""

    def open(self):
        return self._o

    def ready(self, _a=None):
        return self._r


def test_token_wait_is_empty_when_nothing_waits():
    assert status_block() == {}


def test_token_wait_shows_a_rebinding_worker_and_the_captures_state():
    from relay import token_provider as tp
    tp.hold().hold_reason("w0", _Route(ready=False), "A")       # what _rebind_socket now does
    capture_state().attempt()
    capture_state().fail("RuntimeError: no template", 30)
    blk = status_block()
    assert blk["waiting"] == 1
    cap = blk["capture"]
    assert cap["attempts"] == 1 and cap["failures"] == 1
    assert cap["last_error"].startswith("RuntimeError")
    assert 0 < cap["next_attempt_in_s"] <= 30
    assert cap["last_ok_age_s"] is None


def test_a_goal_that_stopped_being_asked_about_is_dropped_from_the_count():
    t = [0.0]
    h = TokenHold(now=lambda: t[0])
    h.hold_reason("gone", _Route(), "A")
    t[0] = 500.0                                                 # far past HOLD_STALE_S
    h.hold_reason("still", _Route(), "A")
    assert h.summary()["waiting"] == 1


# ---- idle clock -------------------------------------------------------------------------------

def test_a_text_less_tool_marker_counts_as_progress_but_is_not_prose():
    from relay import test_chathub as T
    marker = {"type": 2, "item": {"messages": [
        {"messageType": "ExecutingTool", "author": "bot", "text": ""}]}}
    assert CH.collect_progress(marker) == []
    assert [m["type"] for m in CH.collect_activity(marker)] == ["ExecutingTool"]
    # a Chat message, a user message and a ping are not activity
    assert CH.collect_activity({"type": 2, "item": {"messages": [
        {"messageType": "Chat", "author": "bot", "text": ""}]}}) == []
    assert CH.collect_activity({"type": 2, "item": {"messages": [
        {"messageType": "ExecutingTool", "author": "user", "text": ""}]}}) == []
    assert CH.collect_activity({"type": 6}) == []

    seen = []
    connect = T._connect_returning(["{}" + CH.RS, json.dumps(marker) + CH.RS,
                                    T._update("ok"), T._done()])
    conv = CH.Conversation(lambda: T._token())
    assert conv.ask("x", connect=connect, on_progress=seen.append) == "ok"
    assert [s["type"] for s in seen] == ["ExecutingTool"]
    assert conv.last_progress == []
    assert sum(conv.frame_kinds.values()) >= 2


def test_driver_idle_clock_is_refreshed_by_a_tool_marker_during_a_long_call():
    from relay import socket_driver as SD

    class Conv:
        frame_kinds = {}
        closed = False

        def ask(self, text, *, on_progress=None, **_kw):
            time.sleep(0.25)
            on_progress({"type": "ExecutingTool", "origin": "", "text": ""})
            time.sleep(0.25)
            return "done"

        def close(self):
            self.closed = True

    drv = SD.CopilotSocketDriver(Conv(), connect=object())
    drv.send("q")
    time.sleep(0.3)
    assert drv.generation_idle_s() < 0.2        # 0.3 s into the turn, but 0.05 s since the marker
    deadline = time.time() + 2
    while drv._is_generating() and time.time() < deadline:
        time.sleep(0.01)


def test_a_stall_reports_what_the_stream_was_doing():
    from relay import socket_driver as SD

    class Conv:
        frame_kinds = {"1/update": 3, "2/": 1}

        def ask(self, *a, **k):
            time.sleep(0.5)
            return "x"

        def close(self):
            pass

    drv = SD.CopilotSocketDriver(Conv(), connect=object())
    drv.send("q")
    drv.fail_stalled_turn("socket turn made no meaningful progress for 61s (limit 60s)")
    assert "no meaningful progress" in drv.failed
    assert "1/update x3" in drv.failed

    class Quiet(Conv):
        frame_kinds = {}

    d2 = SD.CopilotSocketDriver(Quiet(), connect=object())
    d2.send("q")
    d2.fail_stalled_turn("no meaningful progress")
    assert "none besides pings" in d2.failed


# ---- stale wording ----------------------------------------------------------------------------

def test_the_route_closed_line_does_not_mention_tabs_as_a_fallback():
    text = Path("relay/socket_route.py").read_text(encoding="utf-8")
    assert "workers now open tabs" not in text
    assert "a capture that fails means workers open tabs" not in text
