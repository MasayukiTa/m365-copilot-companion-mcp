"""mcp_inbound.jsonl: every /mcp arrival leaves one timestamped line, and never a body.

Drives the real main._BearerPrefix (the outermost ASGI layer) around a tiny fake app with a
hand-written ASGI scope, so no socket or uvicorn is involved and nothing is Windows-specific.
The state directory is moved under tmp_path through tools.tool_ledger.LEDGER_PATH, the same
resolution tool_events.jsonl uses.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("MCP_API_KEY", "inbound-log-test-key-do-not-use")

import main  # noqa: E402
from tools import obs_log, tool_ledger  # noqa: E402


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_ledger, "LEDGER_PATH", str(tmp_path / "tool_events.jsonl"))
    monkeypatch.setattr(obs_log, "_WARNED", False)
    return tmp_path


def _rows(state):
    p = state / obs_log.INBOUND_NAME
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]


def _drive(body: bytes, method="POST", headers=None, chunks=1, consume=True):
    """Run one request through _BearerPrefix; returns (status, bytes the inner app read)."""
    seen = {"body": b"", "status": None}
    hdrs = [(b"user-agent", b"X" * 200), (b"mcp-session-id", b"sess-abc-123")]
    hdrs += list(headers or [])
    scope = {"type": "http", "method": method, "path": "/mcp", "headers": hdrs,
             "client": ("203.0.113.9", 4000)}

    async def inner(scope, receive, send):
        if consume:
            while True:
                msg = await receive()
                seen["body"] += msg.get("body", b"")
                if not msg.get("more_body"):
                    break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    size = max(1, len(body) // chunks)
    pieces = [body[i:i + size] for i in range(0, len(body), size)] or [b""]
    queue = [{"type": "http.request", "body": p, "more_body": True} for p in pieces[:-1]]
    queue.append({"type": "http.request", "body": pieces[-1], "more_body": False})

    async def receive():
        return queue.pop(0)

    async def send(message):
        if message["type"] == "http.response.start":
            seen["status"] = message["status"]

    asyncio.run(main._BearerPrefix(inner)(scope, receive, send))
    return seen["status"], seen["body"]


def test_initialize_and_tools_list_are_recorded(state):
    for method in ("initialize", "tools/list", "notifications/initialized", "ping"):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                           "params": {"clientInfo": {"name": "very-private-client-name"}}})
        status, seen = _drive(body.encode())
        assert status == 200
        assert seen == body.encode(), "the inner app must receive the body unchanged"
    rows = _rows(state)
    assert [r["method"] for r in rows] == ["initialize", "tools/list",
                                           "notifications/initialized", "ping"]
    r = rows[0]
    assert isinstance(r["ts"], float) and r["at"]
    assert r["client_ip"] == "203.0.113.9"
    assert len(r["ua"]) == 80
    assert r["session"] and "sess-abc-123" not in r["session"]
    assert "very-private-client-name" not in (state / obs_log.INBOUND_NAME).read_text("utf-8")


def test_tools_call_logs_only_the_tool_name(state):
    secret = "hunter2-SECRET-VALUE"
    body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                       "params": {"name": "unlock", "arguments": {"password": secret,
                                                                  "token": "tok-" + secret}}})
    _drive(body.encode(), chunks=3)
    rows = _rows(state)
    assert len(rows) == 1
    assert rows[0]["method"] == "tools/call" and rows[0]["tool"] == "unlock"
    text = (state / obs_log.INBOUND_NAME).read_text("utf-8")
    assert secret not in text and "arguments" not in text


def test_authorization_header_never_appears(state):
    _drive(b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
           headers=[(b"authorization", b"Bearer SUPER-SECRET-KEY")])
    assert "SUPER-SECRET-KEY" not in (state / obs_log.INBOUND_NAME).read_text("utf-8")


def test_batch_and_garbage_bodies(state):
    _drive(json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                       {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]).encode())
    _drive(b"not json at all")
    assert [r["method"] for r in _rows(state)] == ["initialize", "tools/list", "(unparsed)"]


def test_get_is_recorded_without_reading_anything(state):
    _drive(b"", method="GET")
    assert [r["method"] for r in _rows(state)] == ["HTTP GET"]


def test_unread_body_still_leaves_a_line_with_status(state):
    _drive(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}', consume=False)
    assert [r["method"] for r in _rows(state)] == ["(body-unread status=200)"]


def test_a_write_failure_does_not_break_the_request(state, monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("disk on fire")

    monkeypatch.setattr(obs_log, "_state_dir", boom)
    body = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}'
    for _ in range(3):
        status, seen = _drive(body)
        assert status == 200 and seen == body
    err = capsys.readouterr().err
    assert err.count("obs_log: write failed") == 1, "one stderr note per process, not per request"


def test_oversize_body_is_not_parsed(state, monkeypatch):
    monkeypatch.setattr(obs_log, "MAX_PEEK_BYTES", 64)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"pad": "x" * 500}}).encode()
    status, seen = _drive(body, chunks=4)
    assert status == 200 and seen == body
    assert [r["method"] for r in _rows(state)] == ["(oversize)"]


def test_size_cap_keeps_the_tail_on_line_boundaries(state, monkeypatch):
    monkeypatch.setattr(obs_log, "MAX_BYTES", 2000)
    for i in range(200):
        obs_log.append(obs_log.INBOUND_NAME, {"method": "ping", "n": i})
    path = state / obs_log.INBOUND_NAME
    assert path.stat().st_size < 2000 + 200
    rows = [json.loads(x) for x in path.read_text("utf-8").splitlines()]
    assert rows[-1]["n"] == 199, "the newest line must survive"
    assert rows[0]["n"] > 0, "the oldest lines were dropped"
    assert [r["n"] for r in rows] == list(range(rows[0]["n"], 200)), "no torn or missing lines"
