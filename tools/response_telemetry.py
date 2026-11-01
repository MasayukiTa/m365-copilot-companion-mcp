"""Outermost ASGI response-delivery telemetry for the local tool server.

Why this exists (2026-10-31): a client called `muse` (Python-urllib, reaching the
server through a public Dev Tunnel URL from two changing source networks) reports
responses cut or disconnected, mostly above ~2.1 KB. From the server host the same
calls complete 24/24 up to 8 KB and the tunnel host log shows every response sent in
full. We cannot see what happens to a response AFTER the server hands it to the
transport. The access log has no byte counts and no way to tell the client went away
mid-response. This module is the missing eye: wrapped as the OUTERMOST ASGI layer
(applied AFTER _BearerPrefix, so it sees every byte of every send and every receive),
it records one JSON line per HTTP request with what was declared, what was actually
handed to `send`, and whether the exchange completed or the client vanished.

It MUST NOT change any response: every byte is forwarded unchanged and in order, and
every bookkeeping error is swallowed. Logging is off the request path -- lines go
through a Queue to a single daemon writer thread, so the request never blocks on disk
and a logging fault can never break a real request.

Switch: MCP_RESPONSE_TELEMETRY (default "1"). When "0"/"false"/"no"/"off" the
wrapper forwards straight through and records nothing.
"""
from __future__ import annotations

import json
import os
import queue
import asyncio
import threading
import time
from datetime import datetime, timezone, timedelta

# JST, so a line read on the operator's machine reads in local wall-clock time.
_JST = timezone(timedelta(hours=9))

_MAX_BYTES = 5 * 1024 * 1024   # rotate at 5 MB
_KEEP = 4                      # keep .1 .. .4 beside the live file

# -- 10-minute in-memory ring, for /health -------------------------------------
_RING_WINDOW_S = 600.0
# Chunk size for streaming a response body out through the telemetry wrapper. Large single-shot
# bodies are split so the _watch coroutine can surface an http.disconnect between chunks and
# interrupt delivery mid-response (recording fewer bytes than declared -> client_gone).
_SEND_CHUNK = 32 * 1024
_ring = []                     # list[(ts_epoch, client_gone: bool, short: bool)]
_ring_lock = threading.Lock()

# -- live in-flight gauge, for /health and the supervisor's drain gate -----------
# A deploy's "stale code" cycle kills the server process; a request an external client is
# still waiting on is lost. The supervisor's old idle test (fleet idle, bridge not busy)
# never counted live HTTP, so it cycled straight through a client mid-response. These two
# numbers are that missing signal: how many HTTP requests are open right now, and how long
# since the most recent one started. In-memory only, mutated under a lock on the request
# path's enter/exit, and read without ever raising (a telemetry fault must never surface to
# a caller or wedge the gate).
_inflight = 0                  # HTTP requests currently open through this wrapper
_last_start_epoch = None       # time.time() when the most recent request started, or None
_inflight_lock = threading.Lock()


def _inflight_enter(now=None):
    """Record that a request just opened. Never raises."""
    global _inflight, _last_start_epoch
    try:
        with _inflight_lock:
            _inflight += 1
            _last_start_epoch = time.time() if now is None else now
    except Exception:
        pass


def _inflight_exit():
    """Record that a request just closed. Never raises; never goes below zero."""
    global _inflight
    try:
        with _inflight_lock:
            if _inflight > 0:
                _inflight -= 1
    except Exception:
        pass


def health_inflight(now=None):
    """The two live-request numbers /health carries, read without ever raising.

    * ``resp_inflight``          -- HTTP requests open through this wrapper right now.
    * ``resp_last_start_age_s``  -- seconds since the most recent request STARTED, rounded
                                    to tenths, or ``None`` when none has started since the
                                    server came up.

    The supervisor reads these to hold off a stale-code cycle while a client is mid-response
    (scripts/stale_server_check.decide_stale_drain). Any fault collapses to the empty, safe
    reading (zero in flight, no recent start), so a telemetry bug can never wedge the gate.
    """
    try:
        if now is None:
            now = time.time()
        with _inflight_lock:
            n = _inflight
            last = _last_start_epoch
        age = None if last is None else round(max(0.0, now - last), 1)
        return {"resp_inflight": int(n), "resp_last_start_age_s": age}
    except Exception:
        return {"resp_inflight": 0, "resp_last_start_age_s": None}


def _truthy(v: str) -> bool:
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


def enabled() -> bool:
    return _truthy(os.environ.get("MCP_RESPONSE_TELEMETRY", "1"))


def _log_path() -> str:
    # .setup/logs/response_telemetry.jsonl under the repo root. This module lives in
    # tools/, so the repo root is its parent's parent.
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    return os.path.join(root, ".setup", "logs", "response_telemetry.jsonl")


class _Writer:
    """One daemon thread draining a queue to the jsonl file, with size rotation.

    The request path only ever calls .submit(dict), which is a non-blocking put. Any
    error inside the thread (disk full, permission, serialisation) is swallowed: a
    telemetry fault must never surface to a caller.
    """

    def __init__(self, path):
        self._path = path
        self._q = queue.Queue(maxsize=10000)
        self._thread = threading.Thread(target=self._run, name="resp-telemetry",
                                         daemon=True)
        self._started = False
        self._lock = threading.Lock()

    def _ensure_started(self):
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            try:
                os.makedirs(os.path.dirname(self._path), exist_ok=True)
            except Exception:
                pass
            self._thread.start()
            self._started = True

    def submit(self, rec):
        self._ensure_started()
        try:
            self._q.put_nowait(rec)
        except Exception:
            # Queue full or any other fault: drop the line rather than block or raise.
            pass

    def _rotate_if_needed(self):
        try:
            if os.path.getsize(self._path) < _MAX_BYTES:
                return
        except OSError:
            return
        # path -> path.1, path.1 -> path.2, ... dropping the oldest.
        try:
            oldest = "%s.%d" % (self._path, _KEEP)
            if os.path.exists(oldest):
                os.remove(oldest)
            for i in range(_KEEP - 1, 0, -1):
                src = "%s.%d" % (self._path, i)
                dst = "%s.%d" % (self._path, i + 1)
                if os.path.exists(src):
                    os.replace(src, dst)
            if os.path.exists(self._path):
                os.replace(self._path, "%s.1" % self._path)
        except Exception:
            pass

    def _run(self):
        while True:
            try:
                rec = self._q.get()
            except Exception:
                continue
            try:
                self._rotate_if_needed()
                line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except Exception:
                # Swallow: never let a logging error escape the writer thread.
                pass


_writer_singleton = None
_writer_lock = threading.Lock()


def _writer():
    global _writer_singleton
    if _writer_singleton is None:
        with _writer_lock:
            if _writer_singleton is None:
                _writer_singleton = _Writer(_log_path())
    return _writer_singleton


def _ring_add(ts_epoch, client_gone, short):
    with _ring_lock:
        _ring.append((ts_epoch, bool(client_gone), bool(short)))
        cutoff = ts_epoch - _RING_WINDOW_S
        # Drop anything older than the window from the front. Each row is a
        # (ts_epoch, client_gone, short) tuple, so compare the timestamp element, not the
        # whole tuple (comparing a tuple to a float raises TypeError, which -- swallowed in
        # the finally -- silently dropped EVERY telemetry line before this fix).
        while _ring and _ring[0][0] < cutoff:
            _ring.pop(0)


def health_counters(now=None):
    """The three new /health numbers, over the last 10 minutes."""
    if now is None:
        now = time.time()
    cutoff = now - _RING_WINDOW_S
    with _ring_lock:
        rows = [r for r in _ring if r[0] >= cutoff]
    return {
        "resp_total_10m": len(rows),
        "resp_client_gone_10m": sum(1 for r in rows if r[1]),
        "resp_short_10m": sum(1 for r in rows if r[2]),
    }


def _first_xff(headers):
    for (k, v) in (headers or []):
        if k.lower() == b"x-forwarded-for":
            try:
                raw = v.decode("latin-1")
            except Exception:
                raw = ""
            return raw.split(",")[0].strip()
    return ""


def _header(headers, name):
    for (k, v) in (headers or []):
        if k.lower() == name:
            try:
                return v.decode("latin-1")
            except Exception:
                return ""
    return ""


def _client_ip(scope):
    client = scope.get("client")
    peer = client[0] if client else ""
    # When the peer is loopback (the devtunnel host forwards from localhost), the real
    # origin is the first X-Forwarded-For entry; otherwise the peer address is it.
    if peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        xff = _first_xff(scope.get("headers"))
        return xff or peer
    return peer


class ResponseTelemetry:
    """Outermost ASGI wrapper. Records one JSON line per HTTP request; never alters
    the response. Non-HTTP scopes (lifespan, websocket) pass straight through."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not enabled():
            await self.app(scope, receive, send)
            return

        t0 = time.time()
        method = scope.get("method", "")
        path = scope.get("path", "") or ""
        # Mark this request open for the live gauge /health exposes, EXCEPT the supervisor's
        # own /health poll and the unauthenticated /exec_status probe. The gate reads
        # /health to decide whether to cycle; if that poll counted itself, resp_inflight
        # would never fall to zero and the gate would defer forever. These are local,
        # sub-millisecond monitoring reads, never the external client responses the gate
        # exists to protect. Undone in the finally below.
        gauge = path not in ("/health", "/exec_status")
        if gauge:
            _inflight_enter(t0)
        headers = scope.get("headers")
        ua = _header(headers, b"user-agent")[:40]
        ip = _client_ip(scope)

        state = {
            "status": 0,
            "declared": -1,
            "sent": 0,
            "final_seen": False,     # final body message (more_body false) observed
            "body_started": False,
            "is_sse": False,
            "error": False,
            "disconnected": False,   # http.disconnect from receive before completion
        }

        # uvicorn on this host accepts an entire single-shot body into its transport buffer
        # and send() returns cleanly even after the peer is gone, so a broken pipe never
        # surfaces to the app. The ONLY reliable signal that the client vanished mid-response
        # is an http.disconnect delivered to receive(). A normal single-shot GET handler never
        # calls receive(), so a watcher coroutine drives receive() itself, records the
        # disconnect, and re-publishes every message on an internal queue so handlers that DO
        # call receive() (request bodies, SSE) still get an unchanged, in-order stream.
        inbox = asyncio.Queue()

        async def _watch():
            try:
                while True:
                    message = await receive()
                    try:
                        if (message.get("type") == "http.disconnect"
                                and not state["final_seen"]):
                            state["disconnected"] = True
                    except Exception:
                        pass
                    await inbox.put(message)
                    if message.get("type") == "http.disconnect":
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                return

        async def _recv():
            return await inbox.get()

        async def _send(message):
            mtype = message.get("type")
            try:
                if mtype == "http.response.start":
                    state["status"] = message.get("status", 0)
                    dcl = -1
                    for (k, v) in (message.get("headers") or []):
                        lk = k.lower()
                        if lk == b"content-length":
                            try:
                                dcl = int(v.decode("latin-1").strip())
                            except Exception:
                                dcl = -1
                        elif lk == b"content-type" and b"text/event-stream" in v.lower():
                            state["is_sse"] = True
                    state["declared"] = dcl
            except Exception:
                pass

            if mtype != "http.response.body":
                # Forward everything that is not a body message untouched.
                try:
                    await send(message)
                except Exception:
                    state["error"] = True
                    raise
                return

            state["body_started"] = True
            body = message.get("body") or b""
            more_body = bool(message.get("more_body", False))
            is_final = not more_body

            # Stream the body out in chunks, yielding between chunks so _watch can run and
            # surface a pending http.disconnect. If the client has gone, stop early: fewer
            # bytes than declared are handed off and final_seen stays False -> client_gone.
            # A small/complete body goes out in one or a few chunks with no disconnect and is
            # still recorded complete.
            if not body:
                try:
                    await send(message)
                except Exception:
                    state["error"] = True
                    raise
                if is_final:
                    state["final_seen"] = True
                return

            view = memoryview(body)
            total = len(view)
            off = 0
            while off < total:
                if state["disconnected"]:
                    return
                end = min(off + _SEND_CHUNK, total)
                last_chunk = (end == total)
                out = {
                    "type": "http.response.body",
                    "body": bytes(view[off:end]),
                    "more_body": (not last_chunk) or more_body,
                }
                try:
                    await send(out)
                except Exception:
                    state["error"] = True
                    raise
                state["sent"] += (end - off)
                off = end
                await asyncio.sleep(0)

            if is_final:
                state["final_seen"] = True

        watcher = asyncio.ensure_future(_watch())
        try:
            await self.app(scope, _recv, _send)
        finally:
            # Give the watcher a brief moment to observe a disconnect that raced with the
            # final send, then stop it.
            if not watcher.done():
                try:
                    await asyncio.wait({watcher}, timeout=0.25)
                except Exception:
                    pass
            if not watcher.done():
                watcher.cancel()
                try:
                    await watcher
                except Exception:
                    pass
            try:
                self._record(t0, method, path, ip, ua, state)
            except Exception:
                pass
            # Request is no longer open. In the finally so a handler error, a disconnect,
            # or a telemetry fault above all still decrement the gauge -- a leaked count
            # would make the supervisor defer a stale-code cycle forever.
            if gauge:
                _inflight_exit()

    def _record(self, t0, method, path, ip, ua, state):
        duration_ms = round((time.time() - t0) * 1000.0, 1)
        declared = state["declared"]
        sent = state["sent"]

        if state["error"]:
            end_reason = "error"
            completed = False
        elif state["is_sse"]:
            # Streaming responses end when the stream ends; record the bytes so far.
            end_reason = "stream"
            completed = bool(state["final_seen"])
        elif state["disconnected"] and not state["final_seen"]:
            end_reason = "client_gone"
            completed = False
        elif state["final_seen"]:
            end_reason = "complete"
            completed = True
        else:
            # Body never finished and no explicit error/disconnect surfaced.
            end_reason = "client_gone"
            completed = False

        short = (not completed) or (declared >= 0 and sent < declared)

        rec = {
            "ts": datetime.now(_JST).isoformat(),
            "method": method,
            "path": path,
            "status": state["status"],
            "client_ip": ip,
            "user_agent": ua,
            "declared_content_length": declared,
            "body_bytes_sent": sent,
            "completed": completed,
            "end_reason": end_reason,
            "duration_ms": duration_ms,
        }
        _ring_add(t0, end_reason == "client_gone", short)
        _writer().submit(rec)


# The task names this wrapper _ResponseTelemetry (the outermost ASGI layer, applied last).
# ResponseTelemetry is kept as the public name; this alias lets callers and tests use either.
_ResponseTelemetry = ResponseTelemetry

