# -*- coding: utf-8 -*-
"""relay/mcp_ready.py: creation gate, initialize detector, reply trigger, worker wiring.
All sources are fakes: no network, no .fleet, no process."""
from __future__ import annotations

import json
import types

import pytest

from relay import mcp_ready as mr
from relay import stuck_cause as sc


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# --- gate ------------------------------------------------------------------------------------
GATE_CASES = [
    # id, health doc, tunnel result, expect ok, reason fragment
    ("down", None, (True, ""), False, "waiting for MCP server (not reachable)"),
    ("status_not_ok", {"status": "degraded"}, (True, ""), False, "not reachable"),
    ("just_started", {"status": "ok", "server_pid": 7, "server_uptime_s": 3.2}, (True, ""),
     False, "just started: up 3s < 10s, pid 7"),
    ("exactly_min", {"status": "ok", "server_pid": 7, "server_uptime_s": 10}, (True, ""), True, ""),
    ("tunnel_down", {"status": "ok", "server_pid": 7, "server_uptime_s": 50},
     (False, "tunnel not reachable"), False, "tunnel not reachable"),
    ("tunnel_wrong_host", {"status": "ok", "server_pid": 7, "server_uptime_s": 50},
     (False, "tunnel reaches another server (pid 9 != 7)"), False, "another server"),
    ("healthy", {"status": "ok", "server_pid": 7, "server_uptime_s": 50}, (True, ""), True, ""),
    ("no_uptime_field", {"status": "ok", "server_pid": 7}, (True, ""), True, ""),
    ("garbage_uptime", {"status": "ok", "server_uptime_s": "x"}, (True, ""), True, ""),
]


@pytest.mark.parametrize("cid,doc,tun,ok,frag", GATE_CASES, ids=[c[0] for c in GATE_CASES])
def test_gate_table(cid, doc, tun, ok, frag):
    g = mr.ReadyGate(health_fn=lambda: doc, tunnel_fn=lambda pid: tun, now=Clock(),
                     min_uptime_s=10, recheck_s=5)
    v = g.check()
    assert v.ok is ok
    assert frag in v.reason
    assert (g.reason() == "") is ok


def test_gate_raising_probes_mean_not_ready():
    def boom():
        raise RuntimeError("x")
    assert not mr.ReadyGate(health_fn=boom, now=Clock()).check().ok
    g = mr.ReadyGate(health_fn=lambda: {"status": "ok", "server_uptime_s": 99},
                     tunnel_fn=lambda pid: 1 / 0, now=Clock())
    v = g.check()
    assert not v.ok and "tunnel" in v.reason


def test_gate_caches_for_recheck_interval_then_rechecks():
    clk, calls, docs = Clock(), [], [None, {"status": "ok", "server_uptime_s": 60}]

    def health():
        calls.append(clk.t)
        return docs[min(len(calls) - 1, 1)]
    g = mr.ReadyGate(health_fn=health, tunnel_fn=lambda p: (True, ""), now=clk, recheck_s=5)
    for _ in range(24):                       # 24 workers in the same moment: one probe
        assert g.reason()
    assert len(calls) == 1
    clk.t += 4.9
    assert g.reason() and len(calls) == 1     # still cached
    clk.t += 0.2
    assert g.reason() == "" and len(calls) == 2   # 5 s passed: re-probed, server is back


def test_default_tunnel_without_tunnel_configured_is_ok(monkeypatch):
    monkeypatch.delenv("MCP_TUNNEL_URL", raising=False)
    import relay.relay_fleet as rf
    monkeypatch.setattr(rf, "_mcp_tunnel_url", lambda: "")
    assert mr.default_tunnel(7) == (True, "")


def test_default_tunnel_checks_pid(monkeypatch):
    from relay import tool_bridge
    monkeypatch.setattr(tool_bridge, "is_bridge", lambda path=None: False)   # the connector route (a new PC defaults to connector-less)
    import relay.relay_fleet as rf
    monkeypatch.setattr(rf, "_mcp_tunnel_url", lambda: "https://t.example/")
    monkeypatch.setattr(mr, "fetch_json", lambda url, timeout_s=3.0: {"status": "ok", "server_pid": 9})
    assert mr.default_tunnel(7)[0] is False
    assert mr.default_tunnel(9) == (True, "")
    monkeypatch.setattr(mr, "fetch_json", lambda url, timeout_s=3.0: None)
    assert mr.default_tunnel(9) == (False, "tunnel not reachable")


# --- detector --------------------------------------------------------------------------------
def init_row(ts, ua="CopilotStudio PowerFx/1.99.0-local", method="initialize"):
    return {"ts": ts, "method": method, "ua": ua, "session": "abc"}


def make_det(rows, clk, window=45):
    box = list(rows)

    def src():
        out, box[:] = list(box), []
        return out
    det = mr.InitDetector(source=src, now=clk, window_s=window)
    det._box = box
    return det


DET_CASES = [
    # id, sends, inbound rows, now, expected status per worker
    ("init_5s_after", {"w0": 100}, [init_row(105)], 110, {"w0": "attached"}),
    ("nothing_yet", {"w0": 100}, [], 120, {"w0": "waiting"}),
    ("nothing_after_window", {"w0": 100}, [], 146, {"w0": "missing"}),
    ("tools_list_only_is_not_init", {"w0": 100}, [init_row(105, method="tools/list")], 150,
     {"w0": "missing"}),
    ("other_client_ua", {"w0": 100}, [init_row(105, ua="claude-code/2.1")], 150, {"w0": "missing"}),
    ("init_before_send_ignored", {"w0": 100}, [init_row(90)], 150, {"w0": "missing"}),
    ("late_init_after_window_still_counts_if_seen", {"w0": 100}, [init_row(140)], 150,
     {"w0": "attached"}),
    ("counting_3_sends_2_inits", {"w0": 100, "w1": 101, "w2": 102},
     [init_row(105), init_row(106)], 150, {"w0": "attached", "w1": "attached", "w2": "missing"}),
    ("counting_3_sends_3_inits", {"w0": 100, "w1": 101, "w2": 102},
     [init_row(105), init_row(106), init_row(107)], 110,
     {"w0": "attached", "w1": "attached", "w2": "attached"}),
]


@pytest.mark.parametrize("cid,sends,rows,now,want", DET_CASES, ids=[c[0] for c in DET_CASES])
def test_detector_table(cid, sends, rows, now, want):
    clk = Clock(now)
    det = make_det(rows, clk)
    for n, t in sends.items():
        det.expect(n, t)
    assert {n: det.status(n) for n in sends} == want


def test_detector_untracked_and_forget():
    det = make_det([], Clock(100))
    assert det.status("nobody") == "untracked"
    det.expect("w", 90)
    det.forget("w")
    assert det.status("w") == "untracked"


def test_detector_init_arrives_in_a_later_poll():
    clk = Clock(100)
    det = make_det([], clk)
    det.expect("w", 100)
    assert det.status("w") == "waiting"
    det._box.append(init_row(105))
    clk.t = 106
    assert det.status("w") == "attached"
    clk.t = 400
    assert det.status("w") == "attached"      # a fact, not re-judged


def test_detector_one_init_serves_one_conversation():
    clk = Clock(110)
    det = make_det([init_row(105)], clk)
    det.expect("a", 100)
    assert det.status("a") == "attached"
    det.expect("b", 109)                      # a second conversation, no new init
    clk.t = 160
    assert det.status("b") == "missing"
    assert det.status("a") == "attached"


# --- decide_tools: recycle only if the server was healthy for the whole window -----------
def gate_with(doc):
    return mr.ReadyGate(health_fn=lambda: doc, tunnel_fn=lambda p: (True, ""), now=Clock(),
                        recheck_s=0)


DECIDE_CASES = [
    ("healthy_whole_window", {"status": "ok", "server_uptime_s": 500}, "recycle"),
    ("server_restarted_inside_window", {"status": "ok", "server_uptime_s": 20}, "rearmed"),
    ("server_down", None, "rearmed"),
    ("uptime_unknown_but_ok", {"status": "ok"}, "recycle"),
]


@pytest.mark.parametrize("cid,doc,want", DECIDE_CASES, ids=[c[0] for c in DECIDE_CASES])
def test_decide_tools_table(cid, doc, want):
    clk = Clock(200)
    det = make_det([], clk)
    det.expect("w", 100)                       # 100 s ago, window 45 s -> missing
    assert mr.decide_tools(det, gate_with(doc), "w", now=200) == want
    if want == "rearmed":                      # window restarts from now
        assert det.sent_at("w") == 200
        assert mr.decide_tools(det, gate_with(doc), "w", now=210) == "waiting"


def test_decide_tools_passthrough_states():
    clk = Clock(110)
    det = make_det([init_row(105)], clk)
    det.expect("a", 100)
    assert mr.decide_tools(det, gate_with(None), "a", now=110) == "attached"
    assert mr.decide_tools(det, gate_with(None), "zz", now=110) == "untracked"
    det.expect("b", 109)
    assert mr.decide_tools(det, gate_with(None), "b", now=112) == "waiting"


# --- inbound tail ----------------------------------------------------------------------------
def test_inbound_tail_is_incremental_and_handles_partial_lines(tmp_path):
    p = tmp_path / "in.jsonl"
    p.write_text(json.dumps(init_row(1)) + "\n" + json.dumps(init_row(2))[:20], encoding="utf-8")
    tail = mr.InboundTail(lambda: str(p))
    assert [r["ts"] for r in tail.poll()] == [1]          # partial 2nd line not parsed
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(init_row(2))[20:] + "\n" + json.dumps(init_row(3)) + "\n")
    assert [r["ts"] for r in tail.poll()] == [2, 3]
    assert tail.poll() == []
    p.write_text(json.dumps(init_row(9)) + "\n", encoding="utf-8")   # truncated/rotated
    assert [r["ts"] for r in tail.poll()] == [9]


def test_inbound_tail_missing_file_is_empty(tmp_path):
    assert mr.InboundTail(lambda: str(tmp_path / "nope.jsonl")).poll() == []


# --- reply trigger ---------------------------------------------------------------------------
REPLY_CASES = [
    ("only three functions", "呼べる関数は UniversalSearchTool / AskTool / CaptureContextTool のみです。", True),
    ("en no call_tool", "I have no call_tool in my function list, so I cannot read the file.", True),
    ("en not available", "The call_tool function is not available in this conversation.", True),
    ("en dont have", "I don't have access to call_tool here.", True),
    ("ja not in list", "関数一覧に call_tool がありません。", True),
    ("ja unusable", "call_tool は使えません。", True),
    ("used it fine", "call_tool を実行して結果を取得しました。", False),
    ("en used it", "I called call_tool and it returned 3 rows.", False),
    ("unrelated", "了解しました。集計を続けます。", False),
    ("empty", "", False),
    ("none", None, False),
]


@pytest.mark.parametrize("cid,text,want", REPLY_CASES, ids=[c[0] for c in REPLY_CASES])
def test_reply_says_no_tools(cid, text, want):
    assert mr.reply_says_no_tools(text) is want


# --- stuck cause -----------------------------------------------------------------------------
def test_stuck_cause_tools_headline_and_stamp_guard():
    c = sc.tools_unattached_cause(since=1_792_000_000.0, attempts=3, via="no MCP initialize")
    assert c.kind == "tools" and c.attributed
    reason = sc.compose_reason(c, "")
    assert reason.startswith("ツール未接続:")
    assert sc.already_stamped(reason)
    assert sc.plausible(c, "", "")
    assert "tools" in sc.KINDS


# --- worker wiring ---------------------------------------------------------------------------
@pytest.fixture
def rf(monkeypatch):
    import relay.relay_fleet as rf
    import relay.token_provider as tp
    monkeypatch.setattr(tp, "_HOLD", tp.TokenHold())
    return rf


@pytest.fixture
def t1000(monkeypatch):
    monkeypatch.setattr(mr.time, "time", lambda: 1000.0)

def make_worker(rf, **kw):
    w = rf.RelayWorker.__new__(rf.RelayWorker)
    w.name, w.turn, w.status, w.reason, w.outcome = "w3", 1, "ready", "", ""
    w.socket, w.drv = True, types.SimpleNamespace(close=lambda: None)
    w._mcp_conv_fresh, w._mcp_tracking, w._tools_recycles, w._tools_first_at = True, False, 0, 0.0
    w._cooldown_until, w._needs_socket_rebind, w._socket_conv_id = 0.0, False, "c1"
    w._last_sent_job, w.job = "GOAL TEXT", "GOAL TEXT"
    w._last_text, w._stable_since, w._count_before, w._t_send = "x", 1, 5, 9.0
    w._socket_turns_seen = 4
    w.stuck_cause, w.stuck_cause_info = "", {}
    w._unsent_noted, w._attached_ts, w.stuck = True, 0.0, False
    for k, v in kw.items():
        setattr(w, k, v)
    return w


def test_worker_held_while_server_down_and_released_when_up(rf, monkeypatch):
    from relay import token_provider as tp
    state = {"doc": None}
    gate = mr.ReadyGate(health_fn=lambda: state["doc"], tunnel_fn=lambda p: (True, ""),
                        now=Clock(), recheck_s=0)
    monkeypatch.setattr(mr, "_GATE", gate)
    w = make_worker(rf)
    assert w._mcp_gate_blocked() is True
    assert w.reason.startswith("waiting for MCP server (not reachable)")
    assert tp.status_block().get("waiting") == 1           # visible in status.json token_wait
    assert w._cooldown_until > 0
    state["doc"] = {"status": "ok", "server_uptime_s": 60}
    assert w._mcp_gate_blocked() is False
    assert tp.status_block() == {}


def test_missing_initialize_recycles_then_goes_stuck_after_bound(rf, monkeypatch, t1000):
    clk = Clock(1000)
    det = make_det([], clk)
    monkeypatch.setattr(mr, "_DET", det)
    monkeypatch.setattr(mr, "_GATE", gate_with({"status": "ok", "server_uptime_s": 9999}))
    w = make_worker(rf)
    for attempt in range(1, mr.MAX_TOOLS_RECYCLES + 1):
        det.expect("w3", 1000 - 100)                      # first send 100 s ago, no initialize
        w._mcp_tracking, w.status = True, "waiting"
        w._check_tools_attached()
        assert w.status == "ready" and w._needs_socket_rebind and w._mcp_conv_fresh
        assert w.drv is None and w._socket_conv_id == ""
        assert w.job == "GOAL TEXT"                       # same goal, first message
        assert str(attempt) in w.reason
    det.expect("w3", 900)
    w._mcp_tracking, w.status = True, "waiting"
    w._check_tools_attached()
    assert w.status == "stuck" and w.outcome == "STUCK"
    assert w.reason.startswith("ツール未接続:") and w.stuck_cause == "tools"


def test_attached_conversation_is_left_alone(rf, monkeypatch, t1000):
    clk = Clock(1000)
    det = make_det([init_row(950)], clk)
    monkeypatch.setattr(mr, "_DET", det)
    monkeypatch.setattr(mr, "_GATE", gate_with({"status": "ok", "server_uptime_s": 9999}))
    det.expect("w3", 940)
    w = make_worker(rf, status="waiting", _mcp_tracking=True)
    w._check_tools_attached()
    assert w.status == "waiting" and not w._mcp_tracking and w._tools_recycles == 0


def test_server_restart_inside_window_does_not_recycle(rf, monkeypatch, t1000):
    clk = Clock(1000)
    det = make_det([], clk)
    monkeypatch.setattr(mr, "_DET", det)
    monkeypatch.setattr(mr, "_GATE", gate_with({"status": "ok", "server_uptime_s": 15}))
    det.expect("w3", 900)
    w = make_worker(rf, status="waiting", _mcp_tracking=True)
    w._check_tools_attached()
    assert w.status == "waiting" and w._tools_recycles == 0
    assert det.sent_at("w3") == 1000.0                     # window re-armed


def test_mid_task_recycle_uses_recycle_job(rf, monkeypatch, t1000):
    monkeypatch.setattr(mr, "_DET", make_det([], Clock(1000)))
    w = make_worker(rf, turn=5)
    w._recycle_job = lambda: "RECYCLE JOB"
    w._recycle_for_missing_tools("the model reports it has no call_tool")
    assert w.job == "RECYCLE JOB" and w.status == "ready" and w._mcp_conv_fresh
