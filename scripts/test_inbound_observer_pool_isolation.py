"""Regression: the inbound ASGI observer must not stall request INTAKE under CPU load.

Background (measured 2026-11-07 09:29-09:31): _observe_inbound journals every request
(_finish -> obs_log.log_inbound, a jsonl write) with asyncio.to_thread, which dispatches
onto the loop's DEFAULT ThreadPoolExecutor -- the SAME pool that runs tool bodies
(pwsh_exec, find_files, run_python, ...). This observer sits on the receive path of EVERY
request, /health included. With CPU at 99.5% and heavy tool bodies holding every default
pool slot, the observer's journalling could not get a worker, so request intake stalled
BEFORE the handler ran. GET /health took 29.6s/25.3s/15.7s/8.7s/19.0s while the loopback
c2c-bridge answered in 278ms. Giving /health's sub-probes their own pool (#461/#463) did
not help, because the request was already stuck in the observer, upstream of the handler.

The fix moves the observer's journalling onto a DEDICATED pool (_OBS_POOL) and makes it
strictly best-effort. These tests pin that contract: _OBS_POOL exists and is its own
bounded pool, and the observer's receive path returns fast even when the DEFAULT executor
is saturated by a real artificial CPU load. They exercise _observe_inbound directly --
no socket, no uvicorn.
"""
import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

# main.py reads MCP_API_KEY at module import (require_api_key -> SystemExit(78) if absent).
# A dummy key lets the module import under pytest; setdefault never overwrites a real key.
os.environ.setdefault("MCP_API_KEY", "test-key-inbound-observer-isolation")

import main  # importing does NOT start the server (uvicorn.run is under __main__)


def test_obs_pool_is_separate_from_default_executor():
    """The inbound-observer pool must be its own ThreadPoolExecutor so a saturated
    default/tool pool cannot starve request intake. Assert it exists, is bounded, and is
    not the default executor or the health pool."""
    import concurrent.futures as f
    assert isinstance(main._OBS_POOL, f.ThreadPoolExecutor)
    # A small, bounded pool -- not an unbounded or shared one.
    assert main._OBS_POOL._max_workers <= 8
    # Distinct object from the health pool and the default executor.
    assert main._OBS_POOL is not main._HEALTH_POOL


def _make_post_scope():
    return {
        "type": "http",
        "method": "POST",
        "client": ("127.0.0.1", 50000),
        "headers": [(b"user-agent", b"pytest")],
    }


def test_observer_receive_stays_fast_under_saturated_default_executor(monkeypatch):
    """THE ROOT-CAUSE REGRESSION, with a real artificial CPU load.

    Reproduce the 2026-11-07 starvation: flood the loop's DEFAULT ThreadPoolExecutor with
    CPU-bound spinners (the stand-in for pwsh_exec/find_files/run_python tool bodies), then
    drive a full request body through _observe_inbound's receive wrapper. Because the
    observer now journals on the DEDICATED _OBS_POOL, the receive path must return fast even
    though the default pool is fully occupied. Before the fix the journalling used the
    default pool and the receive path blocked behind the spinners -- which is how /health's
    intake stalled for tens of seconds.

    To make a regression possible, the journalling (_finish) is forced to take a real pool
    slot for a noticeable time; on the default pool (pre-fix) that slot does not exist under
    saturation, so the await would block on queueing. On the dedicated pool it runs at once.
    """
    # Make obs_log.log_inbound deterministic and observable: it must actually run (on the
    # dedicated pool), and it must not raise. A short sleep stands in for the jsonl write so
    # the test measures queueing/starvation, not disk speed.
    ran = {"count": 0}

    def fake_log_inbound(*a, **k):
        ran["count"] += 1
        time.sleep(0.05)

    import tools.obs_log as obs_log
    monkeypatch.setattr(obs_log, "log_inbound", fake_log_inbound, raising=True)
    monkeypatch.setattr(obs_log, "methods_from_body", lambda b: [("tools/call", "x")], raising=False)
    monkeypatch.setattr(obs_log, "extras_from_body", lambda b: None, raising=False)

    stop = threading.Event()

    def burn():
        x = 0
        while not stop.is_set():
            x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        return x

    loop = asyncio.new_event_loop()
    try:
        # Saturate the loop's DEFAULT executor with real CPU-bound work on every worker.
        default_pool = ThreadPoolExecutor(max_workers=(os.cpu_count() or 4) + 4)
        loop.set_default_executor(default_pool)
        burners = [loop.run_in_executor(None, burn)
                   for _ in range((os.cpu_count() or 4) + 4)]

        # A fake inner receive that delivers one complete request body, then nothing.
        messages = [
            {"type": "http.request", "body": b"{}", "more_body": False},
        ]

        async def inner_receive():
            if messages:
                return messages.pop(0)
            return {"type": "http.disconnect"}

        async def drive():
            recv = await main._observe_inbound(_make_post_scope(), inner_receive)
            t0 = time.time()
            msg = await recv()  # triggers _offload(... ) on the dedicated pool
            return msg, time.time() - t0

        msg, elapsed = loop.run_until_complete(drive())

        # The body is passed through untouched...
        assert msg.get("type") == "http.request"
        # ...and the receive path returned fast despite the default pool being CPU-saturated,
        # because journalling is fire-and-forget on the dedicated _OBS_POOL, not an awaited
        # call on the starved default pool. This is the crux: intake no longer waits on
        # bookkeeping at all.
        assert elapsed < 5.0, (
            "inbound observer must not stall request intake when the default/tool executor "
            f"is CPU-saturated (elapsed={elapsed:.2f}s)")
    finally:
        stop.set()
        for b in burners:
            try:
                b.result(timeout=5)
            except Exception:
                pass
        default_pool.shutdown(wait=False)
        loop.close()

    # Journalling is fire-and-forget: it was SUBMITTED to the dedicated pool (not dropped by a
    # submit failure) and, once CPU load is released, runs. Give the dedicated pool a moment to
    # drain, then confirm it actually ran -- best-effort, but not silently disabled.
    deadline = time.time() + 5.0
    while ran["count"] < 1 and time.time() < deadline:
        time.sleep(0.05)
    assert ran["count"] >= 1, "observer journalling must still run (fire-and-forget) on the dedicated pool"


def test_non_post_request_journals_without_touching_default_pool(monkeypatch):
    """A non-POST request (e.g. GET /health) also journals via the dedicated pool and returns
    the original receive unchanged."""
    import tools.obs_log as obs_log
    seen = {"n": 0}
    monkeypatch.setattr(obs_log, "log_inbound", lambda *a, **k: seen.__setitem__("n", seen["n"] + 1), raising=True)

    async def inner_receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": "GET", "client": ("127.0.0.1", 1), "headers": []}

    async def drive():
        return await main._observe_inbound(scope, inner_receive)

    recv = asyncio.run(drive())
    # For a non-POST the observer returns the ORIGINAL receive (it journals the method line
    # and does not wrap the body stream).
    assert recv is inner_receive
    assert seen["n"] >= 1
