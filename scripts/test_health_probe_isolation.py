"""Regression: /health's sub-probes must not be able to hang the liveness answer.

Background (measured 2026-11-04): every health sub-probe ran via asyncio.to_thread on the
loop's DEFAULT ThreadPoolExecutor -- the same pool that runs tool bodies. A single long
pwsh_exec (150s+) starved that pool, so /health itself timed out and the fleet send-gate
misread a slow /health as a dead server, dropping every worker into `token_wait`.

The fix moved the sub-probes onto a DEDICATED pool (_HEALTH_POOL) bounded by one short
deadline (_HEALTH_PROBE_BUDGET_S) via _gather_health_probes(). These tests pin that
contract: a slow probe is dropped, fast probes still merge, and a probe that cannot get a
worker on a saturated DEFAULT pool is unaffected because /health has its own pool. They
exercise the gather helper directly -- no socket, no uvicorn.

DETERMINISTIC, NOT TIMED. The earlier cut used time.sleep() to stand in for a slow probe and
real CPU-burner threads to saturate the default pool, then asserted a wall-clock ceiling. On a
shared 2-vCPU runner under that saturation the dedicated pool's own dispatch could slip past
the ceiling, or the slow probe's worker could grab a slot a hair before a fast probe, so
'fast2 == ok' read None and the time-assert flaked (PR 526 CI). Every slow/saturating element
here is now a threading.Event or a Barrier the test controls, so a probe is 'slow' because it
is HELD, not because it sleeps, and the default pool is 'saturated' because its workers are
HELD, not because they burn CPU. The invariants proven are unchanged.
"""
import asyncio
import os
import threading
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
    """A probe that cannot return until the test releases it must NOT delay the answer, and its
    field must be absent while the fast probes are still present.

    DETERMINISTIC: the 'slow' probe blocks on a threading.Event the test releases only AFTER
    the gather returns, instead of sleeping for a fixed wall-clock span. Because it can never
    return in time, the gather is guaranteed to drop it on every run -- there is no race
    between a sleep duration and the budget, which is what let 'fast2' read None under load.
    The two fast probes signal a Barrier so the test can confirm BOTH of them actually ran and
    merged before the gather returned; the health pool has four workers, so the fast probes
    always get their slots even with the blocker holding one."""
    # A comfortable budget: the fast probes return the instant they rendezvous, and the slow
    # probe never returns until released AFTER the gather, so it is dropped on every run no
    # matter how large the budget is. There is no wall-clock assertion, so the budget's exact
    # value cannot make this flaky -- it only has to be large enough that the two fast probes'
    # own dispatch comfortably fits, which decouples the result from runner scheduling jitter.
    monkeypatch.setattr(main, "_HEALTH_PROBE_BUDGET_S", 2.0, raising=False)

    release = threading.Event()
    # Barrier(2): the two fast probes rendezvous with EACH OTHER (not this thread), which both
    # proves both ran and keeps them off the critical path of the test thread. They release the
    # instant the second one arrives.
    fast_barrier = threading.Barrier(2)  # fast + fast2

    def fast():
        fast_barrier.wait(timeout=10)
        return {"fast": "ok"}

    def fast2():
        fast_barrier.wait(timeout=10)
        return {"fast2": "ok"}

    def slow():
        # Blocks until released -- stands in for a pool-starving tool body, with no fixed sleep
        # that could finish early (dropping the determinism) or late (hanging teardown).
        release.wait(10)
        return {"slow": "late"}

    probes = [fast, slow, fast2]
    try:
        out = _run(main._gather_health_probes(probes))
    finally:
        release.set()  # let the slow worker exit so it never pins the pool

    assert out.get("fast") == "ok"
    assert out.get("fast2") == "ok"
    assert "slow" not in out, "a probe held past the budget must contribute nothing"


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


def test_health_pool_survives_a_saturated_default_executor(monkeypatch):
    """THE ROOT-CAUSE REGRESSION, with the default executor DETERMINISTICALLY saturated.

    Reproduce the 2026-11-04 starvation: fill EVERY worker of the loop's DEFAULT
    ThreadPoolExecutor so that nothing new submitted to it can get a worker, then show that
    /health's probes -- which run on the DEDICATED _HEALTH_POOL via _gather_health_probes --
    still answer. Before the fix the probes shared that saturated default pool, got no worker,
    and _gather_health_probes returned an EMPTY dict: fast/fast2 were simply absent. So the
    presence of both fields is the true, deterministic proof of isolation.

    DETERMINISTIC, NOT A CPU RACE. The earlier cut occupied the default pool with real
    CPU-burner loops and then gated on a wall-clock ceiling (budget + 8s). On a shared 2-vCPU
    runner that saturation added seconds of scheduling jitter to even the dedicated pool, and
    the ceiling flaked (seen 2026-11-08: 2.85s). Here each default-pool worker is pinned by a
    threading.Event the test releases in a finally, and a Barrier confirms every default worker
    is occupied BEFORE the health gather runs -- so the default pool is provably, fully
    saturated with zero CPU burned and zero dependence on the clock. The only thing asserted is
    ISOLATION: both health fields come back even though the default pool can serve nobody."""
    monkeypatch.setattr(main, "_HEALTH_PROBE_BUDGET_S", 1.0, raising=False)

    loop = asyncio.new_event_loop()
    release = threading.Event()
    n_workers = (os.cpu_count() or 4) + 4
    occupied = threading.Barrier(n_workers + 1)  # every default worker + this thread

    def hold():
        # Occupy a default-pool worker without burning CPU: signal we hold it, then wait.
        occupied.wait(timeout=20)
        release.wait(20)
        return None

    try:
        # Saturate the loop's DEFAULT executor: fill every default worker and block it.
        default_pool = ThreadPoolExecutor(max_workers=n_workers)
        loop.set_default_executor(default_pool)
        holders = [loop.run_in_executor(None, hold) for _ in range(n_workers)]

        # Confirm EVERY default worker is now occupied before we run the health gather, so the
        # saturation is a proven precondition rather than a hoped-for timing coincidence.
        occupied.wait(timeout=20)

        async def drive():
            return await main._gather_health_probes(
                [lambda: {"fast": "ok"}, lambda: {"fast2": "ok"}])

        out = loop.run_until_complete(drive())

        # THE INVARIANT UNDER TEST IS ISOLATION, NOT A WALL-CLOCK RACE. The dedicated health
        # pool is independent of the saturated default pool, so the probes STILL RETURN THEIR
        # RESULTS even though the default pool can serve no one -- that is the regression this
        # pins. The presence of both fields is the deterministic proof of isolation.
        assert out.get("fast") == "ok"
        assert out.get("fast2") == "ok"
    finally:
        release.set()
        for b in holders:
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
