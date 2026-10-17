# -*- coding: utf-8 -*-
"""A conversation waiting on an MCP tool call that runs on THIS machine is working, not stalled.

MEASURED 2026-10-17 22:06-22:15 (socket_route.jsonl, worker w3, turn 2): the turn ran 439.9 s,
made 25 attributed tool calls (busy 207.9 s, longest 90.0 s, a job_wait) and answered DONE -- but
the worker had been failed at +240.4 s by the per-turn budget (`resend_refused`) while job_wait
was open. Separately, `conversation_stall ... no meaningful progress for 171s (limit 60s)`
failed turns whose stream had gone quiet only because a tool call was running.

Neither judge knew about the tool calls, although relay/mcp_attribution.py joins a worker to its
MCP session. These tests drive both judges with an open call in the ledger.
"""
import json
import time
from types import SimpleNamespace

import pytest

from relay import mcp_attribution as attr
from relay import relay_fleet as rf
from relay import settle as S
from relay.socket_route import SocketRoute

SESSION = "a06665cd740a06b7"        # ids copied from the incident; no text
CONV = "64c4624b-3c92-4794-b233-71b3426f7e72"


class _StreamDrv:
    def __init__(self, idle, frame_idle, t0):
        self.idle, self.frame_idle = idle, frame_idle
        self._turn_started_ts = t0
        self.failed, self.stalled = "", False

    def _is_generating(self):
        return True

    def generation_idle_s(self):
        return self.idle

    def frame_idle_s(self):
        return self.frame_idle

    def fail_stalled_turn(self, reason):
        self.failed, self.stalled = reason, True

    def conversation_ids(self):
        return {"client": CONV, "server": "", "session": "", "turns": 2}

    def _answers(self):
        return SimpleNamespace(count=lambda: 0)


def _route(tmp_path):
    r = SocketRoute(enabled=True, connect_fn=object(), now=time.time,
                    log_path=str(tmp_path / "route.jsonl"))
    r.ledger_path = str(tmp_path / "tool_events.jsonl")
    return r


def _bind(route, worker="w3", how="unique", sent_at=None, init_ts=None, session=SESSION):
    route.record_mcp_bound(mcp_session=session, worker=worker, conversation=CONV,
                           server_conversation="", turn_at_bind=1, how=how, candidates=1,
                           sent_at=sent_at, init_ts=init_ts)


def _ledger(route, calls):
    """calls: [(id, tool, start, end|None, timeout_arg|None, session)]"""
    rows = []
    for cid, tool, start, end, tmo, sess in calls:
        args = {"job_id": {"text": "x"}}
        if tmo is not None:
            args["timeout"] = {"text": str(tmo)}
        rows.append({"event": "call", "id": cid, "ts": start, "tool": tool, "args": args,
                     "session": sess})
        if end is not None:
            rows.append({"event": "outcome", "id": cid, "ts": end, "ok": True})
    with open(route.ledger_path, "w", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in rows))


def _worker(route, monkeypatch, drv, name="w3"):
    monkeypatch.setattr(rf, "_socket_route", lambda: route)
    w = rf.RelayWorker("do the thing", name)
    w.socket, w.drv, w.status = True, drv, "waiting"
    w.turn, w._last_sent_job = 2, "do the thing"
    return w


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache = getattr(attr, "_inflight_cache", None)
    if cache is not None:
        cache.update(path=None, at=0.0, spans=[])


def _setup(tmp_path, monkeypatch, calls, how="unique", idle=100.0, frame_idle=90.0, **bind):
    now = time.time()
    route = _route(tmp_path)
    _bind(route, how=how, **bind)
    _ledger(route, [(c[0], c[1], now + c[2], None if c[3] is None else now + c[3], c[4],
                     c[5] if len(c) > 5 else SESSION) for c in calls])
    drv = _StreamDrv(idle, frame_idle, now - 120.0)
    return _worker(route, monkeypatch, drv), drv, now


# ---- the idle judge -----------------------------------------------------------------------------

def test_an_open_attributed_call_is_progress_not_a_stall(tmp_path, monkeypatch):
    w, drv, now = _setup(tmp_path, monkeypatch, [("c1", "job_wait", -60, None, 90)])
    assert w._socket_meaningful_idle_stalled(now) is False
    assert drv.failed == ""


def test_a_call_that_just_finished_still_counts_as_recent_progress(tmp_path, monkeypatch):
    w, drv, now = _setup(tmp_path, monkeypatch, [("c1", "run_python", -80, -10, None)])
    assert w._socket_meaningful_idle_stalled(now) is False


def test_a_call_open_beyond_its_own_timeout_no_longer_counts(tmp_path, monkeypatch):
    # job_wait with timeout 90 (+30 s grace) open for 200 s: hung, the stall stands
    w, drv, now = _setup(tmp_path, monkeypatch, [("c1", "job_wait", -200, None, 90)])
    assert w._socket_meaningful_idle_stalled(now) is True
    assert "no meaningful progress" in drv.failed


def test_default_tool_timeout_applies_when_the_call_gave_none(tmp_path, monkeypatch):
    # shell_exec defaults to 30 s: open 100 s is beyond 30 + grace
    w, drv, now = _setup(tmp_path, monkeypatch, [("c1", "shell_exec", -100, None, None)])
    assert w._socket_meaningful_idle_stalled(now) is True


def test_a_long_explicit_timeout_is_honoured(tmp_path, monkeypatch):
    w, drv, now = _setup(tmp_path, monkeypatch, [("c1", "shell_exec", -400, None, 600)])
    assert w._socket_meaningful_idle_stalled(now) is False


def test_a_finished_call_long_ago_is_not_progress(tmp_path, monkeypatch):
    w, drv, now = _setup(tmp_path, monkeypatch, [("c1", "run_python", -110, -90, None)])
    assert w._socket_meaningful_idle_stalled(now) is True


def test_an_unattributed_worker_behaves_exactly_as_before(tmp_path, monkeypatch):
    route = _route(tmp_path)                       # no binding at all
    now = time.time()
    _ledger(route, [("c1", "job_wait", now - 60, None, 90, SESSION)])
    w = _worker(route, monkeypatch, _StreamDrv(100.0, 90.0, now - 120))
    assert w._socket_meaningful_idle_stalled(now) is True


def test_a_call_on_another_workers_session_does_not_count(tmp_path, monkeypatch):
    w, drv, now = _setup(tmp_path, monkeypatch, [("c1", "job_wait", -60, None, 90, "other")])
    assert w._socket_meaningful_idle_stalled(now) is True


def test_a_fifo_binding_with_consistent_timing_counts(tmp_path, monkeypatch):
    now = time.time()
    w, drv, now = _setup(tmp_path, monkeypatch, [("c1", "job_wait", -60, None, 90)], how="fifo",
                         sent_at=now - 125, init_ts=now - 121)
    assert w._socket_meaningful_idle_stalled(now) is False


def test_a_fifo_call_that_started_before_this_turn_does_not_count(tmp_path, monkeypatch):
    now = time.time()
    # the turn started 120 s ago; the call started 300 s ago: it belongs to someone else
    w, drv, now = _setup(tmp_path, monkeypatch, [("c1", "job_wait", -300, None, 400)], how="fifo",
                         sent_at=now - 125, init_ts=now - 121)
    assert w._socket_meaningful_idle_stalled(now) is True


def test_a_fifo_binding_whose_init_does_not_follow_its_send_does_not_count(tmp_path, monkeypatch):
    now = time.time()
    w, drv, now = _setup(tmp_path, monkeypatch, [("c1", "job_wait", -60, None, 90)], how="fifo",
                         sent_at=now - 400, init_ts=now - 121)
    assert w._socket_meaningful_idle_stalled(now) is True


def test_idle_probe_telemetry_and_healthy_path_are_untouched(tmp_path, monkeypatch):
    w, drv, now = _setup(tmp_path, monkeypatch, [], idle=5.0, frame_idle=1.0)
    assert w._socket_meaningful_idle_stalled(now) is False


# ---- the per-turn budget ------------------------------------------------------------------------

class _Poller:
    poll = rf.RelayWorker.poll
    _socket_tool_idle_s = getattr(rf.RelayWorker, "_socket_tool_idle_s", lambda self, now=None: None)
    _conversation_ids_for_binding = rf.RelayWorker._conversation_ids_for_binding
    status = "waiting"
    socket = True
    name = "w3"
    per_turn_timeout_s = 240
    dwell_s = 4.0
    _count_before = 0
    transient = 0
    max_transient = 10
    last_response = ""

    def __init__(self, drv, sent_ago):
        self.drv = drv
        self._t_send = time.time() - sent_ago
        self._last_text = None
        self._stable_since = None
        self._settle_state = S.SettleState()
        self.retried = False
        self.outcome = None
        self.reason = ""

    def _report_socket_turns(self):
        pass

    def _check_tools_attached(self):
        pass

    def _capture_url(self):
        pass

    def _retry_transient(self):
        self.retried = True
        self.status = "ready"
        self.transient += 1
        return True

    def _note_timeout(self, *a, **k):
        pass

    def _salvage_via_checks(self):
        return False

    def _timeout_resend_decision(self):
        return "resend"


class _QuietDrv(_StreamDrv):
    def _is_generating(self):
        return False


def _poller(tmp_path, monkeypatch, calls, sent_ago=250.0, how="unique"):
    now = time.time()
    route = _route(tmp_path)
    monkeypatch.setattr(rf, "_socket_route", lambda: route)
    _bind(route, how=how)
    _ledger(route, [(c[0], c[1], now + c[2], None if c[3] is None else now + c[3], c[4], SESSION)
                    for c in calls])
    return _Poller(_QuietDrv(0, None, now - sent_ago), sent_ago)


def test_per_turn_budget_is_extended_while_a_call_is_open(tmp_path, monkeypatch):
    w = _poller(tmp_path, monkeypatch, [("c1", "job_wait", -30, None, 90)])
    assert w.poll() is False
    assert w.retried is False and w.status == "waiting"


def test_per_turn_budget_still_ends_a_turn_with_no_tool_activity(tmp_path, monkeypatch):
    w = _poller(tmp_path, monkeypatch, [])
    assert w.poll() is False
    assert w.retried is True


def test_per_turn_budget_still_ends_a_turn_whose_call_is_hung(tmp_path, monkeypatch):
    w = _poller(tmp_path, monkeypatch, [("c1", "job_wait", -240, None, 90)])
    w.poll()
    assert w.retried is True


def test_per_turn_extension_has_an_absolute_ceiling(tmp_path, monkeypatch):
    w = _poller(tmp_path, monkeypatch, [("c1", "job_wait", -30, None, 90)],
                sent_ago=rf.SOCKET_TURN_TIMEOUT_S + 10)
    w.poll()
    assert w.retried is True


# ---- replay of the incident (ids and timestamps only) -------------------------------------------

def test_replay_of_w3_turn_2_open_job_wait_at_plus_240(tmp_path, monkeypatch):
    """t0 and the job_wait span (start +219.97, end +309.99, timeout arg 90) of the 22:06-22:14
    window, evaluated at the moment the per-turn budget fired (+240.4 s)."""
    t0 = 1792242434.883
    route = _route(tmp_path)
    _bind(route)
    _ledger(route, [("r1", "run_in_background", t0 + 214.8, t0 + 214.86, None, SESSION),
                    ("r2", "job_wait", t0 + 219.97, t0 + 309.99, 90, SESSION)])
    spans = attr.recent_spans(route.ledger_path, now=t0 + 240.4)
    idle = attr.attributed_tool_idle_s("w3", CONV, t0, t0 + 240.4, spans, route.mcp_bindings.snapshot())
    assert idle == 0.0
    # the same moment judged before the fix would have seen no tool knowledge at all:
    assert attr.attributed_tool_idle_s("w3", CONV, t0, t0 + 240.4, spans, {}) is None
