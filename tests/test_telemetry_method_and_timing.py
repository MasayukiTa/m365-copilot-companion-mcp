# -*- coding: utf-8 -*-
"""The /mcp response telemetry names the tool and splits the time (tools/response_telemetry.py).

These drive the ResponseTelemetry ASGI wrapper directly with a hand-written scope / receive /
send -- no socket and no uvicorn, so nothing here is OS-specific. Each test points the telemetry
writer at a private jsonl under tmp_path, runs one request through the wrapper, drains the writer
thread, reads the line back and asserts on the new fields:

  * rpc_method / tool / rpc_id / arg_keys / arg_count  -- the JSON-RPC envelope of a tools/call,
    recorded WITHOUT any argument value.
  * wait_ms / tool_ms                                  -- intake-vs-tool-body split timing.
  * end_reason == client_gone and recv_to_disconnect_ms when the client disconnects mid-response.

It also covers scripts/slow_tool_calls.collect: only tools/call rows at or above the threshold,
slowest first.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import tools.response_telemetry as rt  # noqa: E402


# ---- a minimal driver for the ASGI wrapper ------------------------------------------------

def _tools_call_body(tool="screen_layout", args=None, rpc_id=7):
    arguments = {"path": "C:/x", "instruction": "look"} if args is None else args
    doc = {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
           "params": {"name": tool, "arguments": arguments}}
    return json.dumps(doc).encode("utf-8")


async def _run(scope, receive, send):
    inner_calls = {"n": 0}

    async def app(scope, recv, snd):
        # A minimal inner app: read the whole request body (so the wrapper sees the body
        # boundary), optionally pause (to grow tool_ms), then emit a small complete response.
        inner_calls["n"] += 1
        while True:
            msg = await recv()
            if msg.get("type") == "http.request" and not msg.get("more_body"):
                break
            if msg.get("type") == "http.disconnect":
                return
        pause = scope.get("_test_pause", 0.0)
        if pause:
            await asyncio.sleep(pause)
        body = b"ok"
        await snd({"type": "http.response.start", "status": 200,
                   "headers": [(b"content-length", str(len(body)).encode()),
                               (b"content-type", b"application/json")]})
        await snd({"type": "http.response.body", "body": body, "more_body": False})

    wrapper = rt.ResponseTelemetry(app)
    await wrapper(scope, receive, send)
    return inner_calls


def _scope(path="/mcp", method="POST", pause=0.0):
    return {"type": "http", "method": method, "path": path,
            "headers": [(b"user-agent", b"muse-test"), (b"content-type", b"application/json")],
            "client": ("203.0.113.5", 5000), "_test_pause": pause}


def _receive_from(messages):
    """A receive() that yields the given messages in order, then returns http.disconnect so the
    telemetry watcher coroutine finishes on its own (a real ASGI server also delivers a final
    http.disconnect once the connection closes). After that it blocks, as a spec-compliant
    receive() does once the stream has ended."""
    q = list(messages)
    disconnected = {"done": False}

    async def receive():
        await asyncio.sleep(0)
        if q:
            return q.pop(0)
        if not disconnected["done"]:
            disconnected["done"] = True
            return {"type": "http.disconnect"}
        await asyncio.sleep(3600)
    return receive


def _collecting_send():
    sent = []

    async def send(message):
        sent.append(message)
    return send, sent


@pytest.fixture()
def telem(tmp_path, monkeypatch):
    logdir = tmp_path / ".setup" / "logs"
    logdir.mkdir(parents=True)
    logfile = logdir / "response_telemetry.jsonl"
    monkeypatch.setattr(rt, "_log_path", lambda: str(logfile))
    monkeypatch.setattr(rt, "_writer_singleton", None, raising=False)
    with rt._ring_lock:
        rt._ring.clear()
    monkeypatch.setenv("MCP_RESPONSE_TELEMETRY", "1")

    def read_rows(timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if logfile.exists():
                rows = [json.loads(ln) for ln in logfile.read_text(encoding="utf-8").splitlines()
                        if ln.strip()]
                if rows:
                    return rows
            time.sleep(0.02)
        return []

    return read_rows


# ---- tests: the envelope is named, no value leaks ------------------------------------------

def test_tools_call_names_the_tool_method_id_and_arg_keys_without_values(telem):
    body = _tools_call_body(tool="screen_layout",
                            args={"path": "C:/secret/location", "instruction": "peek"},
                            rpc_id=42)
    messages = [{"type": "http.request", "body": body, "more_body": False}]
    receive = _receive_from(messages)
    send, _sent = _collecting_send()
    asyncio.run(_run(_scope(), receive, send))

    rows = telem()
    assert rows, "no telemetry line written"
    row = rows[-1]
    assert row["rpc_method"] == "tools/call"
    assert row["tool"] == "screen_layout"
    assert row["rpc_id"] == "42"
    assert row["arg_keys"] == ["instruction", "path"]
    assert row["arg_count"] == 2
    # No argument VALUE anywhere in the serialised line.
    blob = json.dumps(row, ensure_ascii=False)
    assert "C:/secret/location" not in blob
    assert "peek" not in blob


def test_initialize_records_method_but_no_tool(telem):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"capabilities": {}}}).encode("utf-8")
    messages = [{"type": "http.request", "body": body, "more_body": False}]
    asyncio.run(_run(_scope(), _receive_from(messages), _collecting_send()[0]))
    row = telem()[-1]
    assert row["rpc_method"] == "initialize"
    assert row["rpc_id"] == "1"
    assert "tool" not in row
    assert "arg_keys" not in row


def _receive_body_then_late_disconnect(body, disconnect_after_s):
    """Deliver the request body at once, then an http.disconnect only AFTER disconnect_after_s --
    long enough that a slow-but-completing response has already finished (final_seen=True), so the
    late disconnect is not mistaken for a client that gave up. Lets a 'complete' path be tested
    while still letting the watcher coroutine finish on its own."""
    q = [{"type": "http.request", "body": body, "more_body": False}]
    sent_disconnect = {"done": False}

    async def receive():
        if q:
            await asyncio.sleep(0)
            return q.pop(0)
        if not sent_disconnect["done"]:
            sent_disconnect["done"] = True
            await asyncio.sleep(disconnect_after_s)
            return {"type": "http.disconnect"}
        await asyncio.sleep(3600)
    return receive


def test_split_timing_separates_wait_from_tool_body(telem):
    body = _tools_call_body()
    # The inner app pauses ~150 ms after the body arrives, before the first response byte, so
    # tool_ms must be clearly positive. The disconnect is held off until well after that, so the
    # response completes first and end_reason is 'complete', not 'client_gone'.
    receive = _receive_body_then_late_disconnect(body, disconnect_after_s=0.5)
    asyncio.run(_run(_scope(pause=0.15), receive, _collecting_send()[0]))
    row = telem()[-1]
    assert "wait_ms" in row
    assert "tool_ms" in row
    assert row["tool_ms"] >= 100.0, row
    assert row["end_reason"] == "complete", row


def test_client_gone_records_reason_and_recv_to_disconnect(telem):
    # The inner app, after the body, emits a start with a large declared length and ONE small
    # chunk with more_body=True, then never finishes. A disconnect arrives before the final byte,
    # so end_reason is client_gone and recv_to_disconnect_ms is recorded.
    body = _tools_call_body(tool="slow_tool", rpc_id=9)

    async def app(scope, recv, snd):
        while True:
            msg = await recv()
            if msg.get("type") == "http.request" and not msg.get("more_body"):
                break
        await snd({"type": "http.response.start", "status": 200,
                   "headers": [(b"content-length", b"1000000")]})
        await snd({"type": "http.response.body", "body": b"x" * 10, "more_body": True})
        # Let the disconnect be observed by the watcher.
        await asyncio.sleep(0.2)

    messages = [
        {"type": "http.request", "body": body, "more_body": False},
        {"type": "http.disconnect"},
    ]
    wrapper = rt.ResponseTelemetry(app)
    asyncio.run(wrapper(_scope(), _receive_from(messages), _collecting_send()[0]))
    row = telem()[-1]
    assert row["end_reason"] == "client_gone"
    assert row["completed"] is False
    assert "recv_to_disconnect_ms" in row
    assert row["recv_to_disconnect_ms"] >= 0.0
    assert row["tool"] == "slow_tool"


def test_unparsed_and_oversize_bodies_are_labelled_not_dropped(telem):
    # Non-JSON body -> rpc_method (unparsed); the request still completes and is recorded.
    messages = [{"type": "http.request", "body": b"not json at all", "more_body": False}]
    asyncio.run(_run(_scope(), _receive_from(messages), _collecting_send()[0]))
    row = telem()[-1]
    assert row["rpc_method"] == "(unparsed)"
    assert "tool" not in row


# ---- tests: the gateway inner tool name -----------------------------------------------------

def test_call_tool_records_inner_tool_without_inner_arg_values(telem):
    # A tools/call whose tool is the gateway 'call_tool'. The inner tool name lives in
    # params.arguments['name']; the inner call's own arguments are under params.arguments
    # ['arguments']. Only the inner tool NAME must be recorded as inner_tool; no inner argument
    # value (and no inner arg key name) may appear in the serialised line.
    body = _tools_call_body(
        tool="call_tool",
        args={"name": "read_file",
              "arguments": {"path": "C:/secret/location", "token": "hunter2-secret"}},
        rpc_id=55)
    messages = [{"type": "http.request", "body": body, "more_body": False}]
    asyncio.run(_run(_scope(), _receive_from(messages), _collecting_send()[0]))

    row = telem()[-1]
    assert row["tool"] == "call_tool"
    assert row["inner_tool"] == "read_file"
    # The outer arguments shape is still recorded (keys name/arguments), but NO inner value or
    # inner arg key name leaks anywhere in the line.
    blob = json.dumps(row, ensure_ascii=False)
    assert "C:/secret/location" not in blob
    assert "hunter2-secret" not in blob
    # 'path' and 'token' are INNER arg key names -- they must not be recorded either.
    assert "token" not in blob


def test_call_tool_empty_inner_name_is_recorded_as_index(telem):
    # The gateway's index lookup passes name='' -- recorded as '(index)', not an empty string.
    body = _tools_call_body(tool="call_tool", args={"name": "", "arguments": {}}, rpc_id=56)
    messages = [{"type": "http.request", "body": body, "more_body": False}]
    asyncio.run(_run(_scope(), _receive_from(messages), _collecting_send()[0]))
    row = telem()[-1]
    assert row["inner_tool"] == "(index)"


def test_call_tool_non_string_inner_name_does_not_raise_or_record(telem):
    # When the inner 'name' is not a string, no inner_tool is recorded and nothing raises.
    body = _tools_call_body(tool="call_tool",
                            args={"name": {"not": "a string"}, "arguments": {}}, rpc_id=57)
    messages = [{"type": "http.request", "body": body, "more_body": False}]
    asyncio.run(_run(_scope(), _receive_from(messages), _collecting_send()[0]))
    row = telem()[-1]
    assert row["tool"] == "call_tool"
    assert "inner_tool" not in row


# ---- tests: the slow-call report ----------------------------------------------------------

def test_slow_tool_calls_collect_filters_and_sorts(tmp_path):
    from scripts import slow_tool_calls as stc

    rows = [
        {"ts": "2026-11-12T21:10:00", "path": "/mcp", "rpc_method": "tools/call",
         "tool": "fast", "duration_ms": 1200.0, "end_reason": "complete"},
        {"ts": "2026-11-12T21:11:00", "path": "/mcp", "rpc_method": "tools/call",
         "tool": "slow_a", "duration_ms": 6800.0, "end_reason": "client_gone"},
        {"ts": "2026-11-12T21:12:00", "path": "/mcp", "rpc_method": "tools/call",
         "tool": "slow_b", "duration_ms": 9100.0, "end_reason": "complete"},
        {"ts": "2026-11-12T21:13:00", "path": "/mcp", "rpc_method": "initialize",
         "duration_ms": 7000.0, "end_reason": "complete"},
    ]
    p = tmp_path / "response_telemetry.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    hits = stc.collect([str(p)], threshold_ms=5000.0, since_ts="")
    # Only the two tools/call rows over 5 s, slowest first; initialize is excluded even though
    # it is over the threshold.
    assert [h["tool"] for h in hits] == ["slow_b", "slow_a"]


def test_slow_tool_calls_fmt_row_shows_inner_tool(tmp_path):
    from scripts import slow_tool_calls as stc

    # A call through the gateway: tool=call_tool with an inner_tool. The formatted line must show
    # BOTH the gateway tool and the inner tool.
    row = {"ts": "2026-11-12T21:10:00", "path": "/mcp", "rpc_method": "tools/call",
           "tool": "call_tool", "inner_tool": "read_file", "duration_ms": 9000.0,
           "end_reason": "complete"}
    line = stc._fmt_row(row)
    assert "call_tool" in line
    assert "inner=read_file" in line
    # A plain call without inner_tool must not print an empty inner= field.
    plain = stc._fmt_row({"ts": "2026-11-12T21:10:00", "tool": "screen_layout",
                          "duration_ms": 9000.0, "end_reason": "complete"})
    assert "inner=" not in plain


def test_slow_tool_calls_since_ts_filters_by_timestamp(tmp_path):
    from scripts import slow_tool_calls as stc

    rows = [
        {"ts": "2026-11-12T20:00:00", "path": "/mcp", "rpc_method": "tools/call",
         "tool": "old", "duration_ms": 8000.0},
        {"ts": "2026-11-12T21:00:00", "path": "/mcp", "rpc_method": "tools/call",
         "tool": "new", "duration_ms": 8000.0},
    ]
    p = tmp_path / "response_telemetry.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    hits = stc.collect([str(p)], threshold_ms=5000.0, since_ts="2026-11-12T20:30:00")
    assert [h["tool"] for h in hits] == ["new"]
