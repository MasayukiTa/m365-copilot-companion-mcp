# -*- coding: utf-8 -*-
"""MCP traffic attribution: which worker / conversation an MCP session belongs to, and which MCP
tool calls were in flight during a socket turn. Synthetic inbound / ledger logs only: no network,
no .fleet, no process."""
from __future__ import annotations

import json

import pytest

from relay import mcp_attribution as attr
from relay import mcp_ready as mr
from relay.socket_route import SocketRoute
from tools import obs_log, tool_ledger

UA = "CopilotStudio PowerFx/1.99.0-local"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# --- inbound rows ----------------------------------------------------------------------------
def init(ts, **extra):
    r = {"ts": ts, "method": "initialize", "session": "", "ua": UA, "client_ip": "1.2.3.4"}
    r.update(extra)
    return r


def birth(ts, session, **extra):
    r = {"ts": ts, "method": "notifications/initialized", "session": session, "ua": UA,
         "client_ip": "1.2.3.4"}
    r.update(extra)
    return r


def make_det(rows, clk):
    box = list(rows)

    def src():
        out, box[:] = list(box), []
        return out
    det = mr.InitDetector(source=src, now=clk, window_s=45)
    det._box = box
    return det


# --- session binding by time -----------------------------------------------------------------
def test_unique_send_binds_its_session():
    det = make_det([init(105.0), birth(105.3, "S1")], Clock(110))
    det.expect("w0", 100.0)
    b = det.binding("w0")
    assert b["session"] == "S1" and b["how"] == "unique" and b["candidates"] == 1
    assert b["init_ts"] == 105.0 and b["sent_at"] == 100.0


def test_no_binding_before_initialize_and_empty_session_before_first_session_row():
    det = make_det([], Clock(103))
    det.expect("w0", 100.0)
    assert det.binding("w0") is None
    det._box.append(init(105.0))
    det._now.t = 106
    b = det.binding("w0")
    assert b is not None and b["session"] == ""          # birth row not read yet
    det._box.append(birth(105.4, "S1"))
    assert det.binding("w0")["session"] == "S1"


def test_burst_is_paired_in_send_order_and_flagged_fifo():
    rows = [init(105.0), birth(105.2, "SA"), init(105.5), birth(105.7, "SB"),
            init(106.0), birth(106.3, "SC")]
    det = make_det(rows, Clock(110))
    for name, t in (("w0", 100.0), ("w1", 100.4), ("w2", 100.9)):
        det.expect(name, t)
    got = {n: det.binding(n) for n in ("w0", "w1", "w2")}
    assert [got[n]["session"] for n in ("w0", "w1", "w2")] == ["SA", "SB", "SC"]
    assert all(g["how"] == "fifo" and g["candidates"] == 3 for g in got.values())


def test_far_apart_sends_are_unique_each():
    rows = [init(105.0), birth(105.2, "SA"), init(205.0), birth(205.2, "SB")]
    det = make_det(rows, Clock(210))
    det.expect("w0", 100.0)
    det.expect("w1", 200.0)
    assert (det.binding("w0")["session"], det.binding("w0")["how"]) == ("SA", "unique")
    assert (det.binding("w1")["session"], det.binding("w1")["how"]) == ("SB", "unique")


def test_one_session_row_is_never_given_to_two_conversations():
    det = make_det([init(105.0), birth(105.2, "SA")], Clock(110))
    det.expect("w0", 100.0)
    det.expect("w1", 101.0)
    assert det.binding("w0")["session"] == "SA"
    assert det.binding("w1") is None


def test_later_rows_of_a_known_session_do_not_rebind():
    rows = [init(105.0), birth(105.2, "SA"),
            {"ts": 120.0, "method": "tools/call", "session": "SA", "ua": UA}]
    det = make_det(rows, Clock(130))
    det.expect("w0", 100.0)
    assert det.binding("w0")["session"] == "SA"


# --- session binding by an identifier --------------------------------------------------------
def test_an_identifier_in_the_request_beats_send_order():
    conv_a, conv_b = "11111111-conv-A", "22222222-conv-B"
    # Arrival order is crossed (B's initialize first); the header hash says who is who.
    rows = [init(105.0, hdr_ids={"x-ms-conversation-id": obs_log.id_label(conv_b)}),
            birth(105.2, "SB"),
            init(105.3, hdr_ids={"x-ms-conversation-id": obs_log.id_label(conv_a)}),
            birth(105.5, "SA")]
    det = make_det(rows, Clock(110))
    det.expect("wA", 100.0, ids=[conv_a])
    det.expect("wB", 100.1, ids=[conv_b])
    a, b = det.binding("wA"), det.binding("wB")
    assert (a["session"], a["how"]) == ("SA", "id")
    assert (b["session"], b["how"]) == ("SB", "id")


def test_initialize_params_identifier_counts_too():
    conv = "conv-in-meta"
    rows = [init(105.0, meta_ids={"conversationId": obs_log.id_label(conv)}), birth(105.2, "S9")]
    det = make_det(rows, Clock(110))
    det.expect("w0", 100.0, ids=[conv])
    assert det.binding("w0")["how"] == "id"


# --- what the server logs: names only --------------------------------------------------------
@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_ledger, "LEDGER_PATH", str(tmp_path / "tool_events.jsonl"))
    monkeypatch.setattr(obs_log, "_WARNED", False)
    monkeypatch.setattr(obs_log, "_SEEN_HDR_SETS", set())
    return tmp_path


def _inbound(state):
    p = state / obs_log.INBOUND_NAME
    return [json.loads(x) for x in p.read_text("utf-8").splitlines() if x]


def test_header_names_are_logged_and_credential_values_never():
    names, ids = obs_log.header_info([
        (b"authorization", b"Bearer TOP-SECRET"), (b"cookie", b"sid=SECRET-COOKIE"),
        (b"x-api-key", b"SECRET-KEY"), (b"x-ms-conversation-id", b"conv-123"),
        (b"traceparent", b"00-abc-def-01"), (b"mcp-session-id", b"raw-session"),
        (b"x-ms-client-request-id", b"req-1"), (b"x-ms-auth-token-id", b"nope"),
        (b"user-agent", b"ua")])
    assert "authorization" in names and "cookie" in names and "x-api-key" in names
    assert set(ids) == {"x-ms-conversation-id", "traceparent", "x-ms-client-request-id"}
    assert ids["x-ms-conversation-id"] == obs_log.id_label("conv-123")
    assert "conv-123" not in json.dumps(ids)


def test_inbound_row_carries_header_names_on_initialize_and_new_name_sets(state):
    hdr = [(b"authorization", b"Bearer TOP-SECRET"), (b"x-ms-conversation-id", b"conv-123"),
           (b"user-agent", b"ua")]
    obs_log.log_inbound([("initialize", "")], "", "1.2.3.4", "ua", headers=hdr)
    obs_log.log_inbound([("tools/list", "")], "sess", "1.2.3.4", "ua", headers=hdr)
    obs_log.log_inbound([("tools/list", "")], "sess", "1.2.3.4", "ua", headers=hdr + [(b"x-new", b"v")])
    rows = _inbound(state)
    assert rows[0]["hdr"] == ["authorization", "user-agent", "x-ms-conversation-id"]
    assert "hdr" not in rows[1]                      # same name set, not an initialize
    assert "x-new" in rows[2]["hdr"]                 # a name set not seen before
    text = (state / obs_log.INBOUND_NAME).read_text("utf-8")
    assert "TOP-SECRET" not in text and "conv-123" not in text
    assert rows[1]["hdr_ids"]["x-ms-conversation-id"] == obs_log.id_label("conv-123")


def test_initialize_params_and_meta_logged_as_key_names_and_hashes():
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "clientInfo": {"name": "private-client", "version": "9"},
        "capabilities": {"roots": {}}, "_meta": {"conversationId": "conv-xyz", "progressToken": "TOK",
                                                 "note": "free text"}}}).encode()
    ex = obs_log.extras_from_body(body)[0]
    assert ex["param_keys"] == ["_meta", "capabilities", "clientInfo", "protocolVersion"]
    assert ex["cap_keys"] == ["roots"] and ex["client_keys"] == ["name", "version"]
    assert ex["meta_keys"] == ["conversationId", "note", "progressToken"]
    assert ex["meta_ids"] == {"conversationId": obs_log.id_label("conv-xyz")}
    flat = json.dumps(ex)
    for secret in ("conv-xyz", "TOK", "private-client", "free text"):
        assert secret not in flat


def test_tool_call_arguments_are_never_read():
    body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "t", "arguments": {"password": "hunter2", "conversationId": "c"},
        "_meta": {"progressToken": 5}}}).encode()
    ex = obs_log.extras_from_body(body)[0]
    assert "hunter2" not in json.dumps(ex) and ex == {"meta_keys": ["progressToken"]}


# --- the bound event and the registry --------------------------------------------------------
def make_route(tmp_path, clk):
    r = SocketRoute(enabled=True, log_path=str(tmp_path / "socket_route.jsonl"), now=clk)
    r.ledger_path = str(tmp_path / "tool_events.jsonl")
    return r


def _log(route):
    with open(route.log_path, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def test_bound_event_is_logged_and_survives_a_restart(tmp_path):
    clk = Clock(500.0)
    route = make_route(tmp_path, clk)
    det = make_det([init(105.0), birth(105.2, "S1")], Clock(110))
    det.expect("w3", 100.0)
    fields = attr.bound_fields("w3", "conv-client-1", 1, det.binding("w3"), "conv-server-1")
    route.record_mcp_bound(**fields)
    row = [r for r in _log(route) if r["event"] == "mcp_session_bound"][0]
    assert (row["mcp_session"], row["worker"], row["conversation"], row["turn_at_bind"]) \
        == ("S1", "w3", "conv-client-1", 1)
    assert row["how"] == "unique" and row["server_conversation"] == "conv-server-1"
    # a new process: the registry is rebuilt from the file
    fresh = attr.Registry(lambda: route.log_path)
    assert fresh.snapshot()["S1"]["worker"] == "w3"
    assert attr.session_worker_map(_log(route))["S1"]["conversation"] == "conv-client-1"


def test_unresolved_session_is_not_mapped():
    rows = [{"event": "mcp_session_bound", "mcp_session": "", "worker": "w1"}]
    assert attr.session_worker_map(rows) == {}


def test_a_later_binding_of_the_same_session_wins():
    rows = [{"event": "mcp_session_bound", "mcp_session": "S", "worker": "w1"},
            {"event": "mcp_session_bound", "mcp_session": "S", "worker": "w2"}]
    assert attr.session_worker_map(rows)["S"]["worker"] == "w2"


# --- tool calls in flight --------------------------------------------------------------------
def call(i, ts, session, tool="lookup"):
    return {"event": "call", "id": i, "ts": ts, "tool": tool, "session": session, "worker": ""}


def outcome(i, ts, ok=True):
    return {"event": "outcome", "id": i, "ts": ts, "ok": ok}


BINDINGS = {"S1": {"worker": "w1", "conversation": "c1", "how": "unique", "ts": 90.0},
            "S2": {"worker": "w2", "conversation": "c2", "how": "fifo", "ts": 90.0}}


def test_in_flight_view_for_a_turn_window():
    rows = [
        call("a", 95, "S1"), outcome("a", 110),            # started before the turn, ends inside
        call("b", 120, "S1"), outcome("b", 130),           # inside
        call("c", 125, "S1"), outcome("c", 140),           # overlaps b
        call("d", 150, "S1"),                              # no outcome: open at the end
        call("e", 100, "S2"), outcome("e", 180),           # another worker's session
        call("f", 100, "ZZ"), outcome("f", 170),           # a session nobody bound
        call("g", 10, "S1"), outcome("g", 50),             # before the window
        call("h", 200, "S1"), outcome("h", 210),           # after the window
    ]
    f = attr.turn_tools_fields("w1", 3, "c1", 100.0, 160.0, attr.tool_spans(rows), BINDINGS, 0.0)
    assert f["mcp_sessions"] == ["S1"] and f["bind_how"] == ["unique"]
    assert f["n_calls"] == 4 and f["open_at_end"] == 1
    assert f["other_overlap"] == 1 and f["unbound_overlap"] == 1
    # union of [100,110] [120,140] [150,160]
    assert f["busy_s"] == pytest.approx(40.0)
    assert f["longest_call_s"] == pytest.approx(15.0, abs=0.01) or f["longest_call_s"] >= 15.0
    by = {c["start_s"]: c for c in f["calls"]}
    assert by[-5.0]["end_s"] == 10.0 and by[50.0]["end_s"] is None
    assert f["ledger_covers_window"] is True and f["window_s"] == 60.0


def test_a_stalled_turn_waiting_on_a_long_call_is_visible():
    rows = [call("x", 101, "S1", tool="slow"), outcome("x", 175)]
    f = attr.turn_tools_fields("w1", 1, "c1", 100.0, 160.0, attr.tool_spans(rows), BINDINGS, 50.0)
    assert f["busy_s"] == pytest.approx(59.0) and f["open_at_end"] == 1
    assert f["calls"][0]["tool"] == "slow" and f["calls"][0]["end_s"] == 75.0


def test_abandoned_call_without_outcome_is_not_in_flight():
    rows = [call("old", 100, "S1")]
    f = attr.turn_tools_fields("w1", 1, "c1", 100.0 + 2 * attr.MAX_OPEN_S,
                               100.0 + 2 * attr.MAX_OPEN_S + 60, attr.tool_spans(rows), BINDINGS, 0.0)
    assert f["n_calls"] == 0


def test_conversation_mismatch_falls_back_to_the_workers_latest_binding():
    f = attr.turn_tools_fields("w1", 1, "unknown-conv", 100.0, 160.0,
                               attr.tool_spans([call("a", 110, "S1"), outcome("a", 120)]), BINDINGS, 0.0)
    assert f["n_calls"] == 1


def test_a_recycled_workers_old_session_is_not_counted():
    bind = dict(BINDINGS)
    bind["S1b"] = {"worker": "w1", "conversation": "c1b", "how": "unique", "ts": 150.0}
    rows = [call("old", 110, "S1"), outcome("old", 120), call("new", 155, "S1b"), outcome("new", 158)]
    f = attr.turn_tools_fields("w1", 1, "c1b", 150.0, 160.0, attr.tool_spans(rows), bind, 0.0)
    assert f["n_calls"] == 1 and f["mcp_sessions"] == ["S1b"]


def test_attribute_tool_rows_adds_a_worker_for_bound_sessions_only():
    rows = [dict(call("a", 1, "S1"), worker=""), call("b", 2, "ZZ"), call("c", 3, "")]
    out = attr.attribute_tool_rows(rows, BINDINGS)
    assert out[0]["worker_bound"] == "w1" and out[0]["conversation_bound"] == "c1"
    assert "worker_bound" not in out[1] and "worker_bound" not in out[2]
    assert "worker_bound" not in rows[0]                     # the input is not modified


def test_read_jsonl_tail_drops_a_partial_first_line(tmp_path):
    p = tmp_path / "l.jsonl"
    lines = [json.dumps({"ts": float(i), "pad": "x" * 50}) for i in range(100)]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rows, first = attr.read_jsonl_tail(str(p), 500)
    assert rows and first == rows[0]["ts"] and rows[-1]["ts"] == 99.0
    assert attr.read_jsonl_tail(str(tmp_path / "missing"), 10) == ([], None)


# --- the route writes both records -----------------------------------------------------------
def test_turn_stats_are_followed_by_turn_tools(tmp_path):
    clk = Clock(1160.0)
    route = make_route(tmp_path, clk)
    route.mcp_bindings.add({"mcp_session": "S1", "worker": "w1", "conversation": "c1",
                            "how": "unique", "ts": 1000.0})
    (tmp_path / "tool_events.jsonl").write_text("\n".join(json.dumps(r) for r in [
        call("a", 1110, "S1", tool="slow"), outcome("a", 1190)]) + "\n", encoding="utf-8")
    route.record_turn_stats({"worker": "w1", "turn": 2, "conversation": "c1", "t0": 1100.0,
                             "outcome": "stalled", "total_s": 60.0})
    ev = {r["event"]: r for r in _log(route)}
    assert ev["socket_turn_stats"]["outcome"] == "stalled"
    t = ev["socket_turn_tools"]
    assert t["worker"] == "w1" and t["turn"] == 2 and t["outcome"] == "stalled"
    assert t["n_calls"] == 1 and t["open_at_end"] == 1 and t["busy_s"] == pytest.approx(50.0)


def test_turn_stats_without_a_start_time_write_no_turn_tools(tmp_path):
    route = make_route(tmp_path, Clock(1160.0))
    route.record_turn_stats({"worker": "w1", "turn": 1, "outcome": "done", "t0": None})
    assert [r["event"] for r in _log(route)] == ["socket_turn_stats"]


def test_a_missing_ledger_still_writes_the_stats(tmp_path):
    route = make_route(tmp_path, Clock(1160.0))
    route.ledger_path = str(tmp_path / "nope.jsonl")
    route.record_turn_stats({"worker": "w1", "turn": 1, "outcome": "done", "t0": 1100.0})
    ev = [r["event"] for r in _log(route)]
    assert ev[0] == "socket_turn_stats"


# --- the worker wiring -----------------------------------------------------------------------
def test_worker_writes_one_bound_event_when_attached(monkeypatch, tmp_path):
    import types

    import relay.relay_fleet as rf
    clk = Clock(110.0)
    det = make_det([init(105.0), birth(105.3, "S7")], clk)
    det.expect("w3", 100.0)
    monkeypatch.setattr(mr, "_DET", det)
    route = make_route(tmp_path, clk)
    monkeypatch.setattr(rf, "_socket_route", lambda: route)
    w = rf.RelayWorker.__new__(rf.RelayWorker)
    w.name = "w3"
    w.drv = types.SimpleNamespace(
        conversation_ids=lambda: {"client": "c-w3", "server": "s-w3", "session": "x", "turns": 1},
        _turn_seq=1)
    assert w._note_mcp_bound() is True
    rows = [r for r in _log(route) if r["event"] == "mcp_session_bound"]
    assert len(rows) == 1
    assert (rows[0]["mcp_session"], rows[0]["worker"], rows[0]["conversation"],
            rows[0]["turn_at_bind"]) == ("S7", "w3", "c-w3", 1)


def test_worker_waits_a_moment_for_the_session_row_then_gives_up(monkeypatch, tmp_path):
    import types

    import relay.relay_fleet as rf
    clk = Clock(106.0)
    det = make_det([init(105.0)], clk)                         # no session row yet
    det.expect("w3", 100.0)
    monkeypatch.setattr(mr, "_DET", det)
    route = make_route(tmp_path, clk)
    monkeypatch.setattr(rf, "_socket_route", lambda: route)
    monkeypatch.setattr(rf.time, "time", lambda: clk.t)
    w = rf.RelayWorker.__new__(rf.RelayWorker)
    w.name = "w3"
    w.drv = types.SimpleNamespace(conversation_ids=lambda: {"client": "c"}, _turn_seq=1)
    assert w._note_mcp_bound() is False
    clk.t = 120.0
    assert w._note_mcp_bound() is True
    row = [r for r in _log(route) if r["event"] == "mcp_session_bound"][0]
    assert row["mcp_session"] == ""                            # logged as unresolved, never mapped
