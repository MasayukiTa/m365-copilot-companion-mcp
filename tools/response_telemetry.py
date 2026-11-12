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


# Largest request body peeked for its JSON-RPC envelope. A bigger one is summarised as
# {"method": "(oversize)"} and its bytes are dropped, so the peek can never grow the request's
# memory without bound. 1 MB matches tools.obs_log.MAX_PEEK_BYTES.
_MAX_PEEK_BYTES = 1024 * 1024


def _rpc_summary(body):
    """Summarise a JSON-RPC request body for the telemetry line WITHOUT recording any argument
    value. Returns a dict with, for the first message of a single or batch body:

      * ``rpc_method``  -- the JSON-RPC ``method`` (e.g. ``tools/call``, ``initialize``), or
                           ``(unparsed)`` when the body is not JSON.
      * ``rpc_id``      -- the JSON-RPC ``id`` as a string (so a cut call can be matched to the
                           client's request), when present.
      * ``tool``        -- for ``tools/call`` only, ``params.name`` (the tool being invoked).
      * ``arg_keys``    -- for ``tools/call`` only, the SORTED KEY NAMES of ``params.arguments``
                           (never any value), so a reader can see the shape of the call.
      * ``arg_count``   -- for ``tools/call`` only, how many argument keys there were.
      * ``rpc_batch``   -- the number of messages, only when the body is a JSON-RPC batch (>1).

    Only method names, the tool name, the id, and argument KEY NAMES/COUNT are ever read; a
    secret passed as an argument VALUE never reaches this function's output. Never raises -- any
    fault collapses to an empty dict so telemetry can never break a request.
    """
    try:
        if not body:
            return {}
        if len(body) > _MAX_PEEK_BYTES:
            return {"rpc_method": "(oversize)"}
        try:
            doc = json.loads(bytes(body).decode("utf-8", "replace"))
        except Exception:
            return {"rpc_method": "(unparsed)"}
        msgs = doc if isinstance(doc, list) else [doc]
        out = {}
        if isinstance(doc, list) and len(msgs) > 1:
            out["rpc_batch"] = len(msgs)
        first = None
        for m in msgs:
            if isinstance(m, dict):
                first = m
                break
        if first is None:
            out["rpc_method"] = "(non-object)"
            return out
        method = str(first.get("method") or "(response)")[:80]
        out["rpc_method"] = method
        rid = first.get("id")
        if rid is not None and not isinstance(rid, (dict, list)):
            out["rpc_id"] = str(rid)[:80]
        if method == "tools/call":
            params = first.get("params")
            if isinstance(params, dict):
                name = params.get("name")
                if name is not None:
                    out["tool"] = str(name)[:80]
                args = params.get("arguments")
                if isinstance(args, dict):
                    keys = sorted(str(k)[:60] for k in args)[:40]
                    out["arg_keys"] = keys
                    out["arg_count"] = len(args)
        return out
    except Exception:
        return {}


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
            # --- request-envelope summary and split timing (added 2026-11-12) ---
            "req_buf": bytearray(),  # request body peeked for its JSON-RPC envelope (<= _MAX_PEEK_BYTES)
            "req_over": False,       # request body exceeded the peek cap; bytes dropped
            "req_done": False,       # whole request body received (more_body false seen)
            "rpc": None,             # dict from _rpc_summary once the body is complete
            "t_req_done": None,      # time.time() when the request body finished arriving
            "t_resp_start": None,    # time.time() of the first http.response.start handed to send
            "t_disconnect": None,    # time.time() when http.disconnect was observed
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
                        mt = message.get("type")
                        if mt == "http.request":
                            # Peek the request body for its JSON-RPC envelope only (method, tool
                            # name, id, argument KEY NAMES). Argument values are never read.
                            # Bounded by _MAX_PEEK_BYTES so the peek cannot grow memory without
                            # bound; past the cap the buffer is dropped and the call is summarised
                            # as (oversize).
                            if not state["req_done"]:
                                chunk = message.get("body") or b""
                                if not state["req_over"]:
                                    if len(state["req_buf"]) + len(chunk) > _MAX_PEEK_BYTES:
                                        state["req_over"] = True
                                        state["req_buf"] = bytearray()
                                    else:
                                        state["req_buf"].extend(chunk)
                                if not message.get("more_body"):
                                    state["req_done"] = True
                                    state["t_req_done"] = time.time()
                                    if state["req_over"]:
                                        state["rpc"] = {"rpc_method": "(oversize)"}
                                    else:
                                        state["rpc"] = _rpc_summary(state["req_buf"])
                                    state["req_buf"] = bytearray()
                        elif mt == "http.disconnect":
                            if state["t_disconnect"] is None:
                                state["t_disconnect"] = time.time()
                            if not state["final_seen"]:
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
                    if state["t_resp_start"] is None:
                        # First byte the app hands back. The gap from t_req_done to here is the
                        # tool body + handler time; the gap from t0 to t_req_done is intake /
                        # network wait. Recording both lets a reader tell a slow tool from a slow
                        # client/transport.
                        state["t_resp_start"] = time.time()
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
                except BaseException:
                    # Awaiting a just-cancelled task re-raises CancelledError, which is a
                    # BaseException (NOT an Exception) on 3.8+. Catching only Exception let it
                    # escape the wrapper and fail the whole request whenever the watcher was still
                    # blocked in receive() at response end -- the common keep-alive case where no
                    # http.disconnect has arrived yet. Swallow every stop-the-watcher outcome: the
                    # response is already delivered and the watcher is pure bookkeeping.
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
        t_end = time.time()
        duration_ms = round((t_end - t0) * 1000.0, 1)
        declared = state["declared"]
        sent = state["sent"]

        # Split the wall-clock duration into intake/network wait and tool-body time so a slow
        # tool can be told apart from a slow client or transport:
        #   wait_ms : t0 -> request body fully received (intake + reading the request off the
        #             socket; the client's upload / network leg).
        #   tool_ms : request body received -> first response byte handed to send (FastMCP
        #             auth + the tool body itself; the server's own work).
        # Either is None when its boundary was never observed (e.g. a GET with no body, or a
        # request the app never answered).
        t_req_done = state.get("t_req_done")
        t_resp_start = state.get("t_resp_start")
        wait_ms = None
        tool_ms = None
        if t_req_done is not None:
            wait_ms = round((t_req_done - t0) * 1000.0, 1)
            if t_resp_start is not None and t_resp_start >= t_req_done:
                tool_ms = round((t_resp_start - t_req_done) * 1000.0, 1)
        elif t_resp_start is not None:
            # No request body boundary seen (e.g. GET): everything up to the first response byte
            # is the server's own work.
            tool_ms = round((t_resp_start - t0) * 1000.0, 1)

        # When the client vanished mid-response, how long from the request body arriving to the
        # disconnect being observed -- i.e. how long the client waited before giving up.
        recv_to_disconnect_ms = None
        t_disc = state.get("t_disconnect")
        if t_disc is not None:
            base = t_req_done if t_req_done is not None else t0
            recv_to_disconnect_ms = round(max(0.0, t_disc - base) * 1000.0, 1)

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
        # Split timing (ms). Present when the relevant boundary was observed.
        if wait_ms is not None:
            rec["wait_ms"] = wait_ms
        if tool_ms is not None:
            rec["tool_ms"] = tool_ms
        if recv_to_disconnect_ms is not None:
            rec["recv_to_disconnect_ms"] = recv_to_disconnect_ms
        # JSON-RPC envelope summary for /mcp (method, tool name, id, argument KEY NAMES/COUNT).
        # Only ever on a path that carried a JSON-RPC body; never any argument value.
        rpc = state.get("rpc")
        if isinstance(rpc, dict):
            rec.update(rpc)
        _ring_add(t0, end_reason == "client_gone", short)
        _writer().submit(rec)


# The task names this wrapper _ResponseTelemetry (the outermost ASGI layer, applied last).
# ResponseTelemetry is kept as the public name; this alias lets callers and tests use either.
_ResponseTelemetry = ResponseTelemetry

