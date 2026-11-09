"""Response-delivery telemetry (tools/response_telemetry.py) and the size-diagnosis
endpoint (main.diag_bytes), driven over real HTTP.

Why over real HTTP and not just by calling the handlers: the whole point of this feature is
to see what happens to a response AFTER the server hands it to the transport -- a client that
reads part of a large response and then closes the socket. That can only be exercised by an
actual socket, so each test starts the ASGI app in-process on an ephemeral port in a daemon
thread (uvicorn) and talks to it with urllib / raw sockets, exactly as the muse client does.

The outermost wrapper under test is tools.response_telemetry.ResponseTelemetry (the task's
_ResponseTelemetry). It is applied LAST, around a tiny Starlette app whose routes stand in for
the real server's shapes: a small complete body, an 8 KB complete body, a 64 KB body the
client abandons mid-stream, an SSE stream, plus the real main.diag_bytes and main.health
handlers mounted as routes so /diag/bytes and /health are exercised through the same wrapper
and recorded by the same telemetry.

The telemetry writes JSONL to <repo>/.setup/logs/response_telemetry.jsonl. Each test points
that at a private temp directory via MCP_* env + a fresh writer, reads the lines back, and
asserts on the recorded fields. Rotation is exercised directly against the writer.
"""
from __future__ import annotations

import json
import os
import asyncio
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# main.py reads MCP_API_KEY at import time; set a known value before importing it so the
# /health full-payload branch is reachable for a local peer without the real operator key.
os.environ.setdefault("MCP_API_KEY", "resp-telemetry-test-key")

import main  # noqa: E402
import tools.response_telemetry as rt  # noqa: E402

from starlette.applications import Starlette  # noqa: E402
from starlette.responses import Response, StreamingResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402


# ---- a tiny backend app whose routes stand in for the real server's response shapes --------

async def _small(request):
    return Response(content=b"hello", media_type="text/plain")


async def _eight_kb(request):
    return Response(content=b"x" * 8192, media_type="application/octet-stream")


_BIG_TOTAL = 8 * 1024 * 1024  # 8 MB, declared up front via Content-Length
_BIG_CHUNK = 64 * 1024


async def _large_abandoned(request):
    # Stream the body in many small chunks rather than one shot. A single Response hands the
    # whole payload to uvicorn in one send() that the transport buffers and acknowledges even
    # after the peer is gone, so an abandoned response is indistinguishable from a complete
    # one. Streaming forces a send() per chunk with the event loop turning in between, so once
    # the client vanishes the pending http.disconnect is delivered to receive() (the telemetry
    # watcher sees it) and the remaining chunks are never handed off -- body_bytes_sent stays
    # below the declared Content-Length. The length is declared so the cut is measurable.
    async def gen():
        sent = 0
        while sent < _BIG_TOTAL:
            n = min(_BIG_CHUNK, _BIG_TOTAL - sent)
            yield b"x" * n
            sent += n
            await asyncio.sleep(0)
    return StreamingResponse(
        gen(),
        media_type="application/octet-stream",
        headers={"content-length": str(_BIG_TOTAL)},
    )


async def _sse(request):
    async def gen():
        for i in range(5):
            yield ("data: %d\n\n" % i).encode()
    return StreamingResponse(gen(), media_type="text/event-stream")


def _build_app():
    routes = [
        Route("/small", _small),
        Route("/eight", _eight_kb),
        Route("/big", _large_abandoned),
        Route("/sse", _sse),
        # The real handlers, mounted so they are exercised through the SAME outer wrapper and
        # recorded by the SAME telemetry as everything else.
        Route("/diag/bytes", main.diag_bytes),
        Route("/health", main.health),
    ]
    inner = Starlette(routes=routes)
    return rt.ResponseTelemetry(inner)


class _Server:
    """Run the wrapped app on an ephemeral port in a daemon thread; stop it on exit."""

    def __init__(self, app):
        import uvicorn
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        config = uvicorn.Config(app, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": [self._sock]}, daemon=True)

    def __enter__(self):
        self._thread.start()
        deadline = time.time() + 10
        while not self._server.started and time.time() < deadline:
            time.sleep(0.02)
        if not self._server.started:
            raise RuntimeError("uvicorn did not start in time")
        return self

    def __exit__(self, *exc):
        self._server.should_exit = True
        self._thread.join(timeout=10)

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)


# ---- telemetry log helpers ----------------------------------------------------------------

@pytest.fixture()
def telem(tmp_path, monkeypatch):
    """Point the telemetry at a private jsonl and hand back a reader. Resets the writer and
    the 10-minute ring so each test sees only its own lines."""
    logdir = tmp_path / ".setup" / "logs"
    logdir.mkdir(parents=True)
    logfile = logdir / "response_telemetry.jsonl"
    monkeypatch.setattr(rt, "_log_path", lambda: str(logfile))
    monkeypatch.setattr(rt, "_writer_singleton", None, raising=False)
    with rt._ring_lock:
        rt._ring.clear()
    monkeypatch.setenv("MCP_RESPONSE_TELEMETRY", "1")

    def read_lines(expected_path=None, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if logfile.exists():
                rows = [json.loads(ln) for ln in logfile.read_text(
                    encoding="utf-8").splitlines() if ln.strip()]
                if expected_path is None:
                    if rows:
                        return rows
                else:
                    hit = [r for r in rows if r.get("path") == expected_path]
                    if hit:
                        return rows
            time.sleep(0.05)
        return []

    return read_lines, logfile


def _last_for(rows, path):
    hits = [r for r in rows if r.get("path") == path]
    assert hits, "no telemetry line for %s in %r" % (path, rows)
    return hits[-1]


# ---- tests --------------------------------------------------------------------------------

def test_small_complete_response_is_recorded_complete(telem):
    read_lines, _ = telem
    with _Server(_build_app()) as srv:
        body = urllib.request.urlopen(srv.url("/small"), timeout=10).read()
    assert body == b"hello"
    rec = _last_for(read_lines("/small"), "/small")
    assert rec["completed"] is True
    assert rec["end_reason"] == "complete"
    assert rec["status"] == 200
    assert rec["body_bytes_sent"] == 5
    assert rec["declared_content_length"] == 5


def test_eight_kb_complete_response_is_recorded_complete(telem):
    read_lines, _ = telem
    with _Server(_build_app()) as srv:
        body = urllib.request.urlopen(srv.url("/eight"), timeout=10).read()
    assert len(body) == 8192
    rec = _last_for(read_lines("/eight"), "/eight")
    assert rec["completed"] is True
    assert rec["end_reason"] == "complete"
    assert rec["body_bytes_sent"] == 8192
    assert rec["declared_content_length"] == 8192


def test_client_that_reads_100_of_64k_then_closes_is_client_gone(telem):
    read_lines, _ = telem
    with _Server(_build_app()) as srv:
        # Raw socket so we can read exactly 100 bytes of the body and then close, modelling the
        # muse client vanishing mid-response.
        s = socket.create_connection(("127.0.0.1", srv.port), timeout=10)
        s.sendall(b"GET /big HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        got = b""
        while b"\r\n\r\n" not in got:
            got += s.recv(256)
        # read a little of the body, then abandon the connection
        s.recv(100)
        s.close()
        # give the server a moment to observe the broken pipe on its next send
        time.sleep(0.5)
    rec = _last_for(read_lines("/big"), "/big")
    assert rec["completed"] is False
    assert rec["end_reason"] == "client_gone"
    # fewer bytes were handed off than declared
    assert rec["body_bytes_sent"] < rec["declared_content_length"]


def test_sse_stream_is_recorded_as_stream(telem):
    read_lines, _ = telem
    with _Server(_build_app()) as srv:
        body = urllib.request.urlopen(srv.url("/sse"), timeout=10).read()
    assert b"data: 0" in body
    rec = _last_for(read_lines("/sse"), "/sse")
    assert rec["end_reason"] == "stream"
    assert rec["body_bytes_sent"] > 0


def test_diag_bytes_exact_length_when_token_set(telem, monkeypatch):
    read_lines, _ = telem
    monkeypatch.setenv("MCP_DIAG_TOKEN", "diagtok")
    with _Server(_build_app()) as srv:
        resp = urllib.request.urlopen(srv.url("/diag/bytes?n=4096&t=diagtok"), timeout=10)
        body = resp.read()
        assert resp.headers.get("content-length") == "4096"
        assert resp.headers.get("content-type") == "application/octet-stream"
    assert body == b"x" * 4096
    rec = _last_for(read_lines("/diag/bytes"), "/diag/bytes")
    assert rec["completed"] is True
    assert rec["body_bytes_sent"] == 4096
    assert rec["declared_content_length"] == 4096


def test_diag_bytes_is_404_when_token_unset(telem, monkeypatch):
    read_lines, _ = telem
    monkeypatch.delenv("MCP_DIAG_TOKEN", raising=False)
    with _Server(_build_app()) as srv:
        try:
            urllib.request.urlopen(srv.url("/diag/bytes?n=10&t=anything"), timeout=10)
            status = 200
        except urllib.error.HTTPError as e:
            status = e.code
    assert status == 404


def test_health_carries_the_three_new_counters(telem):
    read_lines, _ = telem
    with _Server(_build_app()) as srv:
        # one prior request so the ring has something, then read /health from a local peer
        urllib.request.urlopen(srv.url("/small"), timeout=10).read()
        time.sleep(0.2)
        body = json.loads(urllib.request.urlopen(srv.url("/health"), timeout=10).read())
    for key in ("resp_total_10m", "resp_client_gone_10m", "resp_short_10m"):
        assert key in body, "missing %s in /health: %r" % (key, body)
    assert body["resp_total_10m"] >= 1


def test_rotation_keeps_four_files(tmp_path, monkeypatch):
    """Drive the writer past the 5 MB threshold several times and confirm it rotates to at
    most .1 .. .4 beside the live file, dropping the oldest."""
    logfile = tmp_path / "response_telemetry.jsonl"
    monkeypatch.setattr(rt, "_MAX_BYTES", 1024)   # small threshold so the test is fast
    monkeypatch.setattr(rt, "_KEEP", 4)
    w = rt._Writer(str(logfile))
    # write directly through the rotation + append path on this thread (no queue race)
    big = {"ts": "t", "method": "GET", "path": "/x", "filler": "y" * 2048}
    for _ in range(12):
        w._rotate_if_needed()
        with open(w._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(big) + "\n")
    rotated = [logfile.with_name(logfile.name + ".%d" % i) for i in range(1, 6)]
    existing = [p.name for p in rotated if p.exists()]
    # at most four rotated files; the fifth (.5) must never be created
    assert not (logfile.with_name(logfile.name + ".5")).exists()
    assert len(existing) <= 4
    assert len(existing) >= 1


# ---- the live in-flight gauge the supervisor's drain gate reads ---------------------------

def test_health_inflight_unit_enter_and_exit_move_the_gauge():
    """health_inflight() counts open requests and ages the most recent start. Driven directly
    (not over HTTP) because enter/exit are the request-path hooks and this asserts their
    bookkeeping in isolation; the over-HTTP test below proves /health carries the keys."""
    before = rt.health_inflight()["resp_inflight"]
    rt._inflight_enter(100.0)
    try:
        snap = rt.health_inflight(now=100.5)
        assert snap["resp_inflight"] == before + 1
        # age is measured from the most recent start, rounded to tenths.
        assert snap["resp_last_start_age_s"] == 0.5
    finally:
        rt._inflight_exit()
    assert rt.health_inflight()["resp_inflight"] == before


def test_health_inflight_never_goes_below_zero():
    # An extra exit (a double-decrement, a telemetry fault) must not drive the gauge negative
    # -- a negative count read as >0 would make the gate wait forever.
    base = rt.health_inflight()["resp_inflight"]
    rt._inflight_exit()
    rt._inflight_exit()
    assert rt.health_inflight()["resp_inflight"] == max(0, base)
    assert rt.health_inflight()["resp_inflight"] >= 0


def test_health_carries_the_inflight_gauge(telem):
    read_lines, _ = telem
    with _Server(_build_app()) as srv:
        body = json.loads(urllib.request.urlopen(srv.url("/health"), timeout=10).read())
    for key in ("resp_inflight", "resp_last_start_age_s"):
        assert key in body, "missing %s in /health: %r" % (key, body)
    # /health is excluded from the gauge (so the supervisor's own poll cannot keep the count
    # above zero and defer a cycle forever), so a quiet server reads zero in flight here.
    assert body["resp_inflight"] == 0
