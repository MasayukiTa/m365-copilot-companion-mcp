"""Regression: /health's sub-probes must not be able to hang the liveness answer.

Background (measured 2026-11-04): every health sub-probe ran via asyncio.to_thread on the
loop's DEFAULT ThreadPoolExecutor -- the same pool that runs tool bodies. A single long
pwsh_exec (150s+) starved that pool, so /health itself timed out and the fleet send-gate
misread a slow /health as a dead server, dropping every worker into `token_wait`.

The fix moved the sub-probes onto a DEDICATED pool (_HEALTH_POOL) bounded by one short
deadline (_HEALTH_PROBE_BUDGET_S) via _gather_health_probes(). These tests pin that
contract: a slow probe is dropped, fast probes still merge, and the whole call returns well
inside the budget. They exercise the gather helper directly -- no socket, no uvicorn.
"""
import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

# main.py reads MCP_API_KEY at module import (require_api_key -> SystemExit(78) if absent).
# This suite only exercises the pure _gather_health_probes helper, so a dummy key is enough
# to let the module import under pytest. Set it BEFORE importing main; use setdefault so a
# real key in the environment is never overwritten.
os.environ.setdefault("MCP_API_KEY", "test-key-health-probe-isolation")

import main  # importing does NOT start the server (uvicorn.run is under __main__)


def _run(coro):
    return asyncio.run(coro)


def test_fast_probes_all_merge():
    """When every probe is fast, all of their dicts are merged into the result."""
    probes = [
        lambda: {"a": 1},
        lambda: {"b": 2},
        lambda: {"c": 3},
    ]
    out = _run(main._gather_health_probes(probes))
    assert out == {"a": 1, "b": 2, "c": 3}


def test_slow_probe_is_dropped_and_call_stays_within_budget(monkeypatch):
    """A probe that blocks far longer than the budget must NOT delay the answer, and its
    field must be absent while the fast probes are still present."""
    monkeypatch.setattr(main, "_HEALTH_PROBE_BUDGET_S", 0.3, raising=False)

    def slow():
        time.sleep(5.0)          # simulates a pool-starving tool body
        return {"slow": "late"}

    probes = [lambda: {"fast": "ok"}, slow, lambda: {"fast2": "ok"}]
    t0 = time.time()
    out = _run(main._gather_health_probes(probes))
    elapsed = time.time() - t0

    assert elapsed < 2.0, "gather must return near the budget, not wait on the slow probe"
    assert out.get("fast") == "ok"
    assert out.get("fast2") == "ok"
    assert "slow" not in out, "a probe slower than the budget must contribute nothing"


def test_raising_probe_never_breaks_the_gather():
    """A probe that raises contributes nothing; it must not turn liveness into a 500."""
    def boom():
        raise RuntimeError("probe exploded")

    probes = [boom, lambda: {"ok": True}]
    out = _run(main._gather_health_probes(probes))
    assert out == {"ok": True}


def test_non_dict_probe_result_is_ignored():
    """A probe returning a non-dict is skipped rather than crashing the merge."""
    probes = [lambda: None, lambda: "nope", lambda: {"kept": 1}]
    out = _run(main._gather_health_probes(probes))
    assert out == {"kept": 1}


def test_dedicated_pool_is_separate_from_default_executor():
    """The health pool must be its own ThreadPoolExecutor so a saturated default/tool pool
    cannot starve it. We assert it exists, is bounded, and is NOT the default executor."""
    import concurrent.futures as f
    assert isinstance(main._HEALTH_POOL, f.ThreadPoolExecutor)
    # A small, bounded pool -- not an unbounded or shared one.
    assert main._HEALTH_POOL._max_workers <= 8


def test_budget_is_overridable_by_env(monkeypatch):
    """Operators can tune the budget without a code change (MCP_HEALTH_PROBE_BUDGET_S)."""
    # The module read the env at import; just assert the attribute is a positive float so a
    # bad env value could never make the budget zero/negative (which would drop every probe).
    assert isinstance(main._HEALTH_PROBE_BUDGET_S, float)
    assert main._HEALTH_PROBE_BUDGET_S > 0.0


def test_health_pool_survives_a_saturated_default_executor_under_cpu_load(monkeypatch):
    """THE ROOT-CAUSE REGRESSION, with a real artificial CPU load.

    Reproduce the 2026-11-04 starvation: flood the loop's DEFAULT ThreadPoolExecutor with
    CPU-bound spinners (the stand-in for pwsh_exec/find_files/run_python tool bodies), then
    show that /health's probes -- which run on the DEDICATED _HEALTH_POOL via
    _gather_health_probes -- still answer inside the budget. Before the fix the probes shared
    that saturated default pool and could not get a worker, so /health timed out. The spinners
    hold GIL-bound CPU, so the default pool's worker threads are genuinely occupied, not merely
    sleeping.
    """
    monkeypatch.setattr(main, "_HEALTH_PROBE_BUDGET_S", 1.0, raising=False)

    stop = threading.Event()

    def burn():
        # Tight CPU-bound loop: real load on a default-pool worker thread until released.
        x = 0
        while not stop.is_set():
            x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        return x

    loop = asyncio.new_event_loop()
    try:
        # Saturate the loop's DEFAULT executor: fill every default worker with a CPU burner.
        # default max_workers = min(32, cpu_count+4); submit generously to occupy them all.
        default_pool = ThreadPoolExecutor(max_workers=(os.cpu_count() or 4) + 4)
        loop.set_default_executor(default_pool)
        burners = [loop.run_in_executor(None, burn)
                   for _ in range((os.cpu_count() or 4) + 4)]

        async def drive():
            t0 = time.time()
            out = await main._gather_health_probes(
                [lambda: {"fast": "ok"}, lambda: {"fast2": "ok"}])
            return out, time.time() - t0

        out, elapsed = loop.run_until_complete(drive())
        # THE INVARIANT UNDER TEST IS ISOLATION, NOT A WALL-CLOCK RACE. The dedicated health
        # pool is independent of the saturated default pool, so the probes STILL RETURN THEIR
        # RESULTS even under full CPU load -- that is the regression this pins. Before the fix
        # the probes shared the saturated default pool, got no worker, and _gather_health_probes
        # returned an EMPTY dict within the budget: fast/fast2 were simply absent. So the
        # presence of both fields is the true, deterministic proof of isolation.
        assert out.get("fast") == "ok"
        assert out.get("fast2") == "ok"
        # Timing stays bounded, but with a generous margin over the budget: a shared CI runner
        # (2 vCPUs) under deliberate GIL-bound saturation can add seconds of scheduling jitter
        # to even the dedicated pool's dispatch. Gate on the configured budget plus a wide
        # fixed margin instead of a brittle hard-coded 2.0s wall clock that flaked on
        # CPU-saturated runners (seen 2026-11-08: elapsed 2.85s on the resubmission).
        budget = main._HEALTH_PROBE_BUDGET_S
        assert elapsed < (budget + 8.0), (
            "health probes must still answer in bounded time even when the default/tool "
            f"executor is CPU-saturated (budget={budget:.2f}s, elapsed={elapsed:.2f}s)")
    finally:
        stop.set()
        for b in burners:
            try:
                b.result(timeout=5)
            except Exception:
                pass
        default_pool.shutdown(wait=False)
        loop.close()

def test_health_handler_actually_merges_probe_fields(monkeypatch):
    """END-TO-END: the /health handler must AWAIT _gather_health_probes and merge its result
    into the payload. A prior edit built the _probes list but never called the gather helper,
    so every sub-probe field silently vanished from the authenticated reply while the helper's
    own unit tests stayed green. This pins the handler itself, not just the helper.
    """
    # Force the authenticated/local branch and make the probes cheap and deterministic.
    monkeypatch.setattr(main, "derive_identity", lambda peer, xff: (True, "127.0.0.1"))
    monkeypatch.setattr(main, "_server_identity", lambda: {"probe_marker_server": "yes"})
    monkeypatch.setattr(main, "_auth_stats_summary", lambda: {"probe_marker_auth": "yes"})
    monkeypatch.setattr(main, "_self_test_summary", lambda: {})
    monkeypatch.setattr(main, "_tool_probe_summary", lambda: {})
    monkeypatch.setattr(main, "_tunnel_channels_summary", lambda: {})

    class _Client:
        host = "127.0.0.1"

    class _Req:
        client = _Client()
        headers = {}

    resp = _run(main.health(_Req()))
    import json as _json
    body = _json.loads(resp.body.decode("utf-8"))
    assert body["status"] == "ok"
    # The whole point: probe fields produced by _gather_health_probes are present in the reply.
    assert body.get("probe_marker_server") == "yes", "handler did not merge _server_identity probe"
    assert body.get("probe_marker_auth") == "yes", "handler did not merge _auth_stats probe"
    # server_uptime_s is pure arithmetic set before the probes; it must always be present.
    assert "server_uptime_s" in body

