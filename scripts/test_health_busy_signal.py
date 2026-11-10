"""Regression: when /health's sub-probes cannot finish inside the budget under load, the
gather helper must return a CLEAR BUSY SIGNAL (not a silent partial), and must CANCEL any
still-queued probe so the tiny health pool is not starved poll after poll.

Background (incident 2026-11-07, 09:29-09:31): the MCP server was alive but GET /health took
seconds at a time across consecutive calls while a loopback bridge on a separate process
answered in well under a second -- the slowness was inside the server. Fleet workers went
STUCK with "All connection attempts failed" because a slow-but-silent /health was
indistinguishable, to the fleet send-gate, from a healthy one: the gate kept treating a
starved server as fine until the connection was finally dropped.

The dedicated-pool + deadline work (#461) keeps /health FAST. This suite pins the second
half of the contract: past the deadline the answer must SAY it is degraded and HOW MANY
probes did not finish, and queued stragglers must be cancelled. These tests exercise the
pure _gather_health_probes helper directly -- no socket, no uvicorn.

DETERMINISTIC, NOT TIMED. An earlier cut asserted wall-clock budgets (elapsed < 2.0s / 2.5s)
and reproduced the saturation with real GIL-bound CPU spinners. Whether a poll missed the
budget, and whether the degraded path was reached inside a fixed wall-clock, depended on how
the runner scheduled those spinners under the GIL -- so the time-asserts flaked and turned
unrelated PRs red on otherwise-correct builds. Every probe that must 'not finish in time' is
now held open on a threading.Event the test releases only AFTER the gather has returned, so
the budget is ALWAYS exceeded by design. The proof that the budget was honoured is not a
clock reading: it is that the gather RETURNED with the fast result present WHILE the blocker
was still blocked (it could not have waited for the blocker, because the blocker is only
released afterwards). No spinner, no wall-clock threshold, no dependence on runner load.
"""
import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

# main.py reads MCP_API_KEY at import (SystemExit(78) if absent). This suite only touches the
# pure helper, so a dummy key is enough; setdefault never overwrites a real key.
os.environ.setdefault("MCP_API_KEY", "test-key-health-busy-signal")

import main  # importing does NOT start the server (uvicorn.run is under __main__)


def _run(coro):
    return asyncio.run(coro)


def test_budget_exceeded_sets_clear_busy_signal(monkeypatch):
    """A probe that cannot finish inside the budget leaves the answer FAST, present with the
    fast fields, and explicitly flagged degraded with a pending count -- not a silent partial.

    DETERMINISTIC, NOT TIMED: the slow probe blocks on a threading.Event the test releases only
    AFTER the gather returns, so it is guaranteed to miss ANY budget. The proof the gather did
    not wait for it is that its field ('slow') is absent from the returned dict while it was
    still blocked -- a fact about the dict's contents, not about elapsed seconds."""
    monkeypatch.setattr(main, "_HEALTH_PROBE_BUDGET_S", 0.05, raising=False)
    release = threading.Event()
    slow_entered = threading.Event()

    def slow():
        slow_entered.set()
        release.wait(10)          # held open past the budget until the test releases it
        return {"slow": "late"}

    probes = [lambda: {"fast": "ok"}, slow]
    try:
        out = _run(main._gather_health_probes(probes))
    finally:
        release.set()             # let the blocked probe worker exit so it never pins the pool

    assert out.get("fast") == "ok"
    assert out.get("health_degraded") is True, "a budget overrun must be announced, not silent"
    assert out.get("health_probes_pending") == 1
    # The slow probe had NOT returned when the gather answered (released only in finally above),
    # so its field is absent -- the deterministic proof the gather returned near the budget
    # instead of waiting on the slow probe.
    assert "slow" not in out
    assert slow_entered.is_set(), "the slow probe never ran"


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
    blockers ever started.

    DETERMINISTIC, NOT TIMED: the blockers are held on a threading.Event released only AFTER
    the gather returns. The proof the gather did not wait for them is that no blocker result
    ('blk') reached the returned dict while they were still blocked -- not a wall-clock reading."""
    monkeypatch.setattr(main, "_HEALTH_PROBE_BUDGET_S", 0.05, raising=False)
    slots = main._HEALTH_POOL._max_workers
    started = []
    started_lock = threading.Lock()
    release = threading.Event()

    def blocker():
        with started_lock:
            started.append(1)
        release.wait(10)         # hold the slot until the test releases it
        return {"blk": 1}

    # Submit well more than there are slots so the surplus must sit in the pool's queue.
    probes = [blocker for _ in range(slots + 4)]
    try:
        out = _run(main._gather_health_probes(probes))
    finally:
        release.set()

    assert out.get("health_degraded") is True
    # At most `slots` blockers could have started; the queued surplus must have been cancelled
    # rather than run, so the number that started never exceeds the pool size.
    assert len(started) <= slots, (
        f"queued stragglers were not cancelled: {len(started)} started for {slots} slots")
    assert out.get("health_probes_pending", 0) >= 1
    # No blocker had returned when the gather answered (released only in finally above), so no
    # blocker result is in the dict -- the deterministic proof the gather returned near the
    # budget even with every slot held, rather than waiting on the blocked probes.
    assert "blk" not in out


def test_busy_signal_while_default_executor_is_fully_occupied(monkeypatch):
    """THE INCIDENT REGRESSION, proved by pool ISOLATION rather than by a CPU spinner.

    The incident was that /health's sub-probes shared the loop's DEFAULT ThreadPoolExecutor --
    the pool that runs tool bodies -- so when that pool was fully committed, /health's probes
    could not get a worker and the handler stalled. The fix gives /health its own dedicated
    _HEALTH_POOL, independent of the default pool. This test pins exactly that isolation, and
    does it DETERMINISTICALLY: instead of burning CPU with GIL-bound spinners and asserting a
    wall-clock budget (the part that flaked), it OCCUPIES every slot of the default executor
    with blockers held open on a threading.Event, then shows that the health gather still runs
    its probes and returns a truthful busy signal while the default pool has zero free slots.

    What is tested is not weakened: the guarantee under test is 'health probes make progress
    and answer even when the default/tool executor is saturated', and a default pool with every
    worker blocked is saturation that cannot resolve until we release it -- a stronger, exactly
    reproducible stand-in for CPU saturation. A separate health probe is held past the budget on
    its own Event so the degraded path is reached deterministically.
    """
    monkeypatch.setattr(main, "_HEALTH_PROBE_BUDGET_S", 0.05, raising=False)

    default_slots = (os.cpu_count() or 4) + 4
    default_pool = ThreadPoolExecutor(max_workers=default_slots)
    occupied = threading.Barrier(default_slots + 1)  # the blockers + this thread
    release_default = threading.Event()
    release_health = threading.Event()
    health_slow_entered = threading.Event()

    def occupy_default():
        # Occupy a default-executor slot and hold it until released. GIL-free: it just waits,
        # which is a deterministic stand-in for 'this worker is unavailable to /health'.
        occupied.wait(timeout=10)
        release_default.wait(10)

    def health_slow():
        # Runs on the DEDICATED _HEALTH_POOL. Held past the budget so the answer is degraded.
        health_slow_entered.set()
        release_health.wait(10)
        return {"slow": "late"}

    loop = asyncio.new_event_loop()
    occupiers = []
    try:
        loop.set_default_executor(default_pool)
        # Fill EVERY default-executor slot and wait until all of them are actually running,
        # so the default pool is provably, fully occupied before the health gather is driven.
        occupiers = [loop.run_in_executor(None, occupy_default) for _ in range(default_slots)]
        occupied.wait(timeout=10)

        async def drive():
            return await main._gather_health_probes([lambda: {"fast": "ok"}, health_slow])

        out = loop.run_until_complete(drive())

        # The fast probe ran and its result survived even though the default/tool executor had
        # zero free workers -- the proof that /health's pool is independent of the saturated
        # default pool (the property whose absence caused the incident).
        assert out.get("fast") == "ok", (
            "health probe did not run while the default executor was fully occupied -- "
            "the dedicated health pool is not isolated")
        assert health_slow_entered.is_set(), "the dedicated-pool health probe never started"
        # And the client sees a truthful busy answer, not a silent partial: the slow probe was
        # still blocked when the gather answered (released only in the finally below).
        assert out.get("health_degraded") is True
        assert out.get("health_probes_pending", 0) >= 1
        assert "slow" not in out, "gather waited for the blocked health probe"
    finally:
        release_health.set()
        release_default.set()
        for f in occupiers:
            try:
                f.result(timeout=5)
            except Exception:
                pass
        default_pool.shutdown(wait=False)
        loop.close()
