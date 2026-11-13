"""Idle streamable-HTTP sessions must expire so max_sessions cannot be exhausted.

BACKGROUND. fastmcp's http_session_idle_timeout defaults to None (sessions never
expire). Clients that never send DELETE piled up until the MCP SDK limit of 10000
open sessions, after which every initialize returned HTTP 503. main.py now passes
session_idle_timeout=_session_idle_timeout_s(). This test proves (1) the helper's
parsing and (2) with a small server and max_sessions=2 that idle sessions expire
and free their slots, while with no timeout the third initialize is refused.
"""
from __future__ import annotations

import ast
import time
from pathlib import Path

from fastmcp import FastMCP
from starlette.testclient import TestClient

MAIN = Path(__file__).resolve().parent.parent / "main.py"


def _load_helper():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_session_idle_timeout_s")
    ns = {"os": __import__("os")}
    exec(compile(ast.Module([fn], []), str(MAIN), "exec"), ns)
    return ns["_session_idle_timeout_s"]


def test_helper_default_and_override():
    f = _load_helper()
    assert f({}) == 1800.0
    assert f({"MCP_SESSION_IDLE_S": "60"}) == 60.0
    for bad in ("0", "-5", "abc", "", "inf", "nan"):
        assert f({"MCP_SESSION_IDLE_S": bad}) == 1800.0


def test_main_passes_timeout_to_http_app():
    src = MAIN.read_text(encoding="utf-8")
    assert "session_idle_timeout=_session_idle_timeout_s()" in src


_INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "t", "version": "1"}},
}
_HDR = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _open(client):
    return client.post("/mcp", json=_INIT, headers=_HDR).status_code


def _app(idle):
    m = FastMCP("t")
    app = m.http_app(path="/mcp", transport="streamable-http", json_response=True,
                     session_idle_timeout=idle)
    return app


def test_idle_sessions_expire_and_free_slots():
    app = _app(1.0)
    with TestClient(app) as c:
        sm = _find_manager(app)
        sm.max_sessions = 2
        assert _open(c) == 200
        assert _open(c) == 200
        assert _open(c) == 503  # full
        time.sleep(2.5)
        assert _open(c) == 200  # slots came back after idle expiry


def test_without_timeout_sessions_stay_and_cap_is_hit():
    app = _app(None)
    with TestClient(app) as c:
        sm = _find_manager(app)
        sm.max_sessions = 2
        assert _open(c) == 200
        assert _open(c) == 200
        time.sleep(2.5)
        assert _open(c) == 503


def _find_manager(app):
    for r in app.routes:
        ep = getattr(r, "endpoint", None) or getattr(r, "app", None)
        sm = getattr(ep, "session_manager", None)
        if sm is not None:
            return sm
    raise AssertionError("session manager not found on any route")
