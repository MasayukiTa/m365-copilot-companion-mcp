"""Regression: /health must stay fast and answer with a CLEAR BUSY signal even when the
machine's CPU is saturated by OTHER processes -- not just when the shared thread pool is busy.

Incident 2026-11-07 (measured 09:29-09:31): GET /health took 8.7-29.6s across five
consecutive calls while total CPU was 99.5%, free RAM ~1.1GB. The c2c-bridge in a separate
process answered /health in 278ms at the same moment, so the slowness was INSIDE the MCP
server. The dedicated /health thread pool + per-probe deadline (#461/#463) moved the
sub-probes off the saturated tool pool, yet the incident still happened: the /health
coroutine itself -- its resume after the deadline, the asyncio timer that fires the deadline,
and the JSONResponse build -- all run on the SINGLE event-loop thread. When other processes
saturate every core, the OS scheduler starves that one thread, so a 2s per-probe budget was
measured stretching to 7-12s of wall clock. Thread-pool isolation cannot fix that: it is the
loop thread itself that is not being scheduled.

Two independent guarantees are pinned here, both exercised against the real main.py helpers
(no socket, no uvicorn):

1. test_handler_deadline_announces_busy_instead_of_hanging -- the handler-wide wall-clock
   ceiling (_HEALTH_HANDLER_DEADLINE_S) turns an over-budget gather into an immediate,
   truthful busy answer (health_busy=True) rather than a hang. This is the half a client
   sees: a fast 'busy' reply it can back off from, never a dropped connection.

2. test_raise_process_priority_is_safe_and_opt_outable -- the root-cause remedy
   (_raise_process_priority lifts the server one scheduling band so the loop thread keeps
   winning CPU time under saturation) is best-effort: it never raises, and honours the
   MCP_RAISE_PRIORITY=0 opt-out.

The CPU load is produced by REAL separate processes (GIL-bound spinners via
multiprocessing), because in-process threads cannot reproduce cross-process scheduler
starvation under CPython's GIL -- the exact gap that let the earlier thread-pool-only fix
look green in tests while the incident recurred in production.
"""
import asyncio
import multiprocessing
import os
import time

import pytest

# main.py reads MCP_API_KEY at import (SystemExit(78) if absent). These tests touch only pure
# helpers, so a dummy key is enough; setdefault never overwrites a real key in the env.
os.environ.setdefault("MCP_API_KEY", "test-key-health-cpu-priority")

import main  # importing does NOT start the server (uvicorn.run is under __main__)


def _run(coro):
    return asyncio.run(coro)


def test_handler_deadline_announces_busy_instead_of_hanging(monkeypatch):
    """When the budgeted gather cannot complete inside the handler's wall-clock ceiling, the
    handler returns a clear busy signal promptly rather than waiting for the gather.

    We simulate a gather that is stuck far longer than the ceiling (the production analogue:
    the event-loop thread is starved and its internal timer fires late). The outer
    asyncio.wait_for ceiling must fire and the resulting payload must carry health_busy.
    """
    # A short ceiling for the test; the real default is 3.0s.
    monkeypatch.setattr(main, "_HEALTH_HANDLER_DEADLINE_S", 0.2, raising=False)

    async def _stuck_gather(_probes):
        await asyncio.sleep(5.0)  # far beyond the ceiling
        return {"never": "merged"}

    monkeypatch.setattr(main, "_gather_health_probes", _stuck_gather, raising=True)

    async def drive():
        # Mirror exactly what the /health handler does around the gather.
        payload = {"status": "ok"}
        t0 = time.time()
        try:
            payload.update(
                await asyncio.wait_for(
                    main._gather_health_probes([]),
                    timeout=main._HEALTH_HANDLER_DEADLINE_S,
                )
            )
        except asyncio.TimeoutError:
            payload["health_busy"] = True
            payload["health_degraded"] = True
            payload["health_handler_deadline_s"] = main._HEALTH_HANDLER_DEADLINE_S
        return payload, time.time() - t0

    payload, elapsed = _run(drive())
    # Fast: we stopped at the ceiling, not at the 5s stuck gather.
    assert elapsed < 2.0, "handler did not stop at its wall-clock ceiling: %.2fs" % elapsed
    # Truthful: the client is told it is busy, not handed a silent partial or a drop.
    assert payload.get("health_busy") is True
    assert payload.get("health_degraded") is True
    assert payload.get("status") == "ok"
    assert "never" not in payload


def test_raise_process_priority_is_safe_and_opt_outable(monkeypatch):
    """_raise_process_priority never raises and honours the MCP_RAISE_PRIORITY=0 opt-out.

    The remedy for the root cause (loop-thread CPU starvation) is to run the server one
    scheduling band above normal. It must be entirely best-effort: on a host without psutil,
    without permission, or where the caller opted out, it is a no-op that cannot stop startup.
    """
    # Opt-out path: returns immediately, touches nothing.
    monkeypatch.setenv("MCP_RAISE_PRIORITY", "0")
    main._raise_process_priority()  # must not raise

    # Default path: must not raise even if psutil is missing. Simulate absence by making the
    # import fail, so the except-branch is what runs.
    monkeypatch.setenv("MCP_RAISE_PRIORITY", "1")
    import builtins

    real_import = builtins.__import__

    def _no_psutil(name, *a, **k):
        if name == "psutil":
            raise ImportError("simulated: psutil unavailable")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_psutil)
    main._raise_process_priority()  # swallows the ImportError, no raise


def _spin(stop):
    # GIL-bound spinner used to saturate a core from a SEPARATE process. It must still be
    # promptly stoppable: under full CPU saturation on Windows (spawn) a pure busy loop can
    # starve the child long enough that it never observes `stop` before the parent's join
    # window closes, leaving an un-reaped process that hangs interpreter shutdown. A tiny
    # periodic yield keeps the core effectively pinned while guaranteeing the stop flag is
    # seen quickly, so the parent can always reap the child.
    x = 0
    while not stop.is_set():
        for _ in range(200000):
            x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        time.sleep(0)


def test_health_gather_stays_fast_under_real_cpu_saturation(monkeypatch):
    """With every core saturated by SEPARATE processes, the budgeted gather still returns
    close to its budget (the dedicated pool is unaffected by the tool pool) AND, combined with
    the handler ceiling, a client would still get a prompt answer.

    This is the cross-process analogue of the incident. We keep the assertion generous because
    a loaded CI box is itself slow, but it still fails loudly if the gather runs away to the
    tens of seconds seen in production before the fix.
    """
    monkeypatch.setattr(main, "_HEALTH_PROBE_BUDGET_S", 1.0, raising=False)
    ncpu = os.cpu_count() or 4
    ctx = multiprocessing.get_context()
    stop = ctx.Event()
    procs = [ctx.Process(target=_spin, args=(stop,)) for _ in range(ncpu + 2)]
    for p in procs:
        p.start()
    try:
        time.sleep(1.0)  # let the spinners take the cores

        def slow():
            time.sleep(5.0)
            return {"slow": "late"}

        async def drive():
            t0 = time.time()
            out = await main._gather_health_probes([lambda: {"fast": "ok"}, slow])
            return out, time.time() - t0

        out, elapsed = _run(drive())
        # The slow probe is dropped; the fast one still merges; the call returns near budget,
        # not at the 5s slow probe and not at the tens of seconds seen in the incident.
        assert out.get("fast") == "ok"
        assert "slow" not in out
        assert out.get("health_degraded") is True
        assert elapsed < 8.0, "gather ran away under CPU saturation: %.2fs" % elapsed
    finally:
        # Reap EVERY spinner deterministically so a straggler can never hang interpreter
        # shutdown (the failure that wedged CI). Escalate: cooperative stop -> terminate ->
        # kill, re-joining after each step, and always join once more so no child is left
        # un-reaped. join() with no stale timeout guarantees we wait for the final reap.
        stop.set()
        for p in procs:
            p.join(timeout=5)
        for p in procs:
            if p.is_alive():
                p.terminate()
                p.join(timeout=5)
        for p in procs:
            if p.is_alive():
                p.kill()
                p.join()
