"""Regression: when /health's sub-probes cannot finish inside the budget under load, the
gather helper must return a CLEAR BUSY SIGNAL (not a silent partial), and must CANCEL any
still-queued probe so the tiny health pool is not starved poll after poll.

Background (incident 2026-11-07, measured 09:29-09:31): with total CPU at 99.5%% the MCP
server's GET /health took 8.7-29.6s across five consecutive calls while the c2c-bridge on a
separate process answered in 278ms -- the slowness was inside the server. Fleet workers
w19/w21/w22 went STUCK with "All connection attempts failed" because a slow-but-silent
/health was indistinguishable, to the fleet send-gate, from a healthy one: the gate kept
treating a starved server as fine until the connection was finally dropped.

The dedicated-pool + deadline work (#461) keeps /health FAST. This suite pins the second
half of the contract: past the deadline the answer must SAY it is degraded and HOW MANY
probes did not finish, and queued stragglers must be cancelled. These tests exercise the
pure _gather_health_probes helper directly -- no socket, no uvicorn. A CPU-load test
(test_busy_signal_under_cpu_saturation) reproduces the saturation with real GIL-bound
spinners rather than sleeps.
"""
import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

# main.py reads MCP_API_KEY at import (SystemExit(78) if absent). This suite only touches the
# pure helper, so a dummy key is enough; setdefault never overwrites a real key.
os.environ.setdefault("MCP_API_KEY", "test-key-health-busy-signal")

import main  # importing does NOT start the server (uvicorn.run is under __main__)


def _run(coro):
    return asyncio.run(coro)


def test_budget_exceeded_sets_clear_busy_signal(monkeypatch):
    """A probe slower than the budget leaves the answer FAST, present with the fast fields,
    and explicitly flagged degraded with a pending count -- not a silent partial."""
    monkeypatch.setattr(main, "_HEALTH_PROBE_BUDGET_S", 0.3, raising=False)

    def slow():
        time.sleep(5.0)          # stand-in for a pool-starving tool body
        return {"slow": "late"}

    probes = [lambda: {"fast": "ok"}, slow]
    t0 = time.time()
    out = _run(main._gather_health_probes(probes))
    elapsed = time.time() - t0

    assert elapsed < 2.0, "gather must return near the budget, not wait on the slow probe"
    assert out.get("fast") == "ok"
    assert out.get("health_degraded") is True, "a budget overrun must be announced, not silent"
    assert out.get("health_probes_pending") == 1
    assert "slow" not in out


def test_all_fast_probes_leave_no_busy_signal():
    """When everything finishes in time the degraded flag must be ABSENT, so a healthy answer
    is never mistaken for a busy one."""
    out = _run(main._gather_health_probes([lambda: {"a": 1}, lambda: {"b": 2}]))
    assert out == {"a": 1, "b": 2}
    assert "health_degraded" not in out
    assert "health_probes_pending" not in out


def test_queued_straggler_is_cancelled_to_free_the_pool(monkeypatch):
    """More probes than pool slots: the ones still QUEUED when the budget expires must be
    cancelled (never started), freeing slots for the next poll. We size the probe count above
    the pool's max_workers so some are guaranteed to be queued, and we count how many of our
    blockers ever started."""
    monkeypatch.setattr(main, "_HEALTH_PROBE_BUDGET_S", 0.3, raising=False)
    slots = main._HEALTH_POOL._max_workers
    started = []
    started_lock = threading.Lock()
    release = threading.Event()

    def blocker():
        with started_lock:
            started.append(1)
        release.wait(5.0)        # hold the slot until the test releases it
        return {"blk": 1}

    # Submit well more than there are slots so the surplus must sit in the pool's queue.
    probes = [blocker for _ in range(slots + 4)]
    try:
        t0 = time.time()
        out = _run(main._gather_health_probes(probes))
        elapsed = time.time() - t0
    finally:
        release.set()

    assert elapsed < 2.0, "gather must return near the budget even with all slots blocked"
    assert out.get("health_degraded") is True
    # At most `slots` blockers could have started; the queued surplus must have been cancelled
    # rather than run, so the number that started never exceeds the pool size.
    assert len(started) <= slots, (
        f"queued stragglers were not cancelled: {len(started)} started for {slots} slots")
    assert out.get("health_probes_pending", 0) >= 1


def test_busy_signal_under_cpu_saturation(monkeypatch):
    """THE INCIDENT REGRESSION, with a real artificial CPU load.

    Saturate the loop's DEFAULT ThreadPoolExecutor (the pool that runs tool bodies) with
    GIL-bound spinners, then show that /health's probes -- which run on the DEDICATED
    _HEALTH_POOL -- still answer inside the budget, AND that when a probe cannot finish the
    answer carries the clear busy signal instead of hanging or dropping. Before the fix the
    probes shared the saturated pool and /health timed out (8.7-29.6s on 2026-11-07).
    """
    monkeypatch.setattr(main, "_HEALTH_PROBE_BUDGET_S", 1.0, raising=False)
    stop = threading.Event()

    def burn():
        x = 0
        while not stop.is_set():
            x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        return x

    loop = asyncio.new_event_loop()
    burners = []
    default_pool = ThreadPoolExecutor(max_workers=(os.cpu_count() or 4) + 4)
    try:
        loop.set_default_executor(default_pool)
        burners = [loop.run_in_executor(None, burn)
                   for _ in range((os.cpu_count() or 4) + 4)]

        def slow():
            time.sleep(5.0)
            return {"slow": "late"}

        async def drive():
            t0 = time.time()
            out = await main._gather_health_probes([lambda: {"fast": "ok"}, slow])
            return out, time.time() - t0

        out, elapsed = loop.run_until_complete(drive())
        # Fast even under full CPU load, because the health pool is independent of the
        # saturated default/tool pool.
        assert elapsed < 2.5, (
            "health must answer near the budget even when the tool executor is CPU-"
            f"saturated (elapsed={elapsed:.2f}s)")
        assert out.get("fast") == "ok"
        # And the client sees a truthful busy answer, not a silent partial.
        assert out.get("health_degraded") is True
        assert out.get("health_probes_pending", 0) >= 1
    finally:
        stop.set()
        for b in burners:
            try:
                b.result(timeout=5)
            except Exception:
                pass
        default_pool.shutdown(wait=False)
        loop.close()
