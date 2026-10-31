"""uvicorn's timeout_keep_alive decides whether an idle, reused connection survives.

BACKGROUND. main.py starts the server with uvicorn.run(...). Before this fix no
timeout_keep_alive was passed, so uvicorn used its default of 5 s and closed any
keep-alive connection that had been idle longer than that. A client that sent
initialize, paused to think, then reused the SAME socket got the connection aborted
(measured 2026-10-31 on this PC: idle 3 s -> OK; idle 6 s, 12 s, 30 s -> aborted,
seen as RemoteDisconnected / WinError 10053). The fix passes timeout_keep_alive from
MCP_KEEPALIVE_S (default 120 s). This test proves the mechanism directly, without
main's auth layer: a tiny ASGI app served by uvicorn on an ephemeral port.

  * default (5 s): initialize, idle 6 s, reuse -> the reuse FAILS.
  * raised (via the same timeout_keep_alive knob): same sequence -> the reuse SUCCEEDS.

To stay fast the test uses keepalive=3 s with a 5 s idle gap instead of the real 120/6.
No socket detail is Windows-specific: a server that closed the idle connection shows up
either as a refused reuse (ConnectionError / RemoteDisconnected / BadStatusLine) on the
next request, which is what we assert.
"""
from __future__ import annotations

import http.client
import socket
import threading
import time

import pytest
import uvicorn


async def _app(scope, receive, send):
    """Minimal ASGI app: answer every POST with a tiny JSON body."""
    if scope["type"] != "http":
        return
    while True:
        msg = await receive()
        if not msg.get("more_body"):
            break
    body = b'{"jsonrpc":"2.0","id":1,"result":{}}'
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class _Server:
    """Run uvicorn in a background thread on an ephemeral port with a given keepalive."""

    def __init__(self, timeout_keep_alive: int):
        # Bind an ephemeral port ourselves so the test never races on a fixed port.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        self._sock = sock
        config = uvicorn.Config(_app, host="127.0.0.1", port=self.port,
                                log_level="warning",
                                timeout_keep_alive=timeout_keep_alive)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run,
                                        kwargs={"sockets": [self._sock]}, daemon=True)

    def __enter__(self):
        self._thread.start()
        for _ in range(100):
            if self._server.started:
                break
            time.sleep(0.05)
        assert self._server.started, "uvicorn did not start in time"
        return self

    def __exit__(self, *exc):
        self._server.should_exit = True
        self._thread.join(timeout=10)


def _initialize(conn: http.client.HTTPConnection) -> int:
    body = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
    conn.request("POST", "/mcp", body=body,
                 headers={"content-type": "application/json",
                          "content-length": str(len(body))})
    resp = conn.getresponse()
    data = resp.read()
    assert data, "no response body"
    return resp.status


def _reuse_after_idle(timeout_keep_alive: int, idle_s: float) -> bool:
    """initialize, idle, reuse the same connection. Return True if the reuse succeeded."""
    with _Server(timeout_keep_alive) as srv:
        conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
        try:
            assert _initialize(conn) == 200, "first request failed"
            time.sleep(idle_s)
            try:
                status = _initialize(conn)  # reuse the SAME connection
                return status == 200
            except (http.client.RemoteDisconnected, http.client.BadStatusLine,
                    ConnectionError, OSError):
                return False
        finally:
            conn.close()


IDLE_GAP_S = 5.0  # longer than the 3 s keepalive below, shorter than the 120 s default


def test_default_short_keepalive_drops_the_idle_reused_connection():
    # Mirrors uvicorn's old default of 5 s, but smaller so the test stays fast:
    # the idle gap (5 s) exceeds the keepalive (3 s), so the server closes the socket.
    assert _reuse_after_idle(timeout_keep_alive=3, idle_s=IDLE_GAP_S) is False, \
        "a keepalive shorter than the idle gap must drop the reused connection"


def test_raised_keepalive_keeps_the_idle_reused_connection():
    # The fix raises the window (MCP_KEEPALIVE_S, default 120 s). Here a keepalive of 30 s
    # comfortably exceeds the 5 s idle gap, so the same reuse now succeeds.
    assert _reuse_after_idle(timeout_keep_alive=30, idle_s=IDLE_GAP_S) is True, \
        "a keepalive longer than the idle gap must keep the reused connection alive"
