"""GWSLOW regression: /health stays fast and gives a CLEAR busy answer when a sub-probe stalls.

Incident (2026-11-07 09:29-09:31): the MCP server was alive but GET /health took 29.6s,
25.3s, 15.7s, 8.7s and 19.0s in five consecutive calls while total CPU was 99.5%. The
c2c-bridge answered /health in 278ms at the same moment, so the slowness was INSIDE the
server, not the network. A slow /health was misread by the fleet send-gate as 'server down',
so workers w19/w21/w22 went STUCK with 'All connection attempts failed' and the supervisor
restarted the server four times in a row.

Two distinct guarantees are pinned here, and they are tested DETERMINISTICALLY. The earlier
cut of this file reproduced the contention with real CPU-spinner threads and then asserted a
wall-clock budget (and that at least one of five polls happened to miss that budget). Whether
a poll missed the budget depended on how the runner scheduled the GIL under those spinners, so
the degraded path was reached only SOMETIMES: the time-assert flaked, and
'no poll surfaced health_degraded under a 0.3s budget + CPU load' turned main red on an
otherwise-correct build. We now drive the exact same guarantees with a threading.Event that
holds a probe open until the test releases it, so the budget is ALWAYS exceeded by design -- no
spinner, no wall-clock threshold, no dependence on runner load:

  1. _gather_health_probes HONORS THE BUDGET AND SAYS IT IS DEGRADED. A blocker probe that
     cannot return until the test releases it is guaranteed to miss any budget, so the merged
     answer keeps the fast probes' results, sets health_degraded=True and reports
     health_probes_pending=<count>, and returns without waiting for the stragglers. That the
     gather RETURNED with the fast result present while the blocker was still held is the
     load-independent proof that the budget was honoured -- it could not have waited for the
     blocker, because the blocker is still blocked when we assert. This is the behaviour that
     turns a starved server's reply from a silent-partial (read as 'down' by the fleet gate)
     into a truthful busy signal. The blocker is released in a finally, so the test never
     leaves a sleeping worker pinned in the pool.

  2. _watched_code_changed NEVER WALKS THE WATCHED TREE ON THE /health PATH ONCE WARM. The
     synchronous tree walk (tools.deploy_freshness.newer_than, hundreds of stat()s) was the
     slow sub-probe on 2026-11-07. The fix is stale-while-revalidate: serve the cached answer
     instantly and refresh off-thread, starting at most ONE background walk no matter how many
     callers hit a stale cache at once. We assert the walk runs on a background thread, never
     the caller's, and that a burst of callers triggers <= 1 walk -- both are load-independent
     facts about WHICH THREAD does the work, not about how long a walk takes. A threading.Event
     gates the walk so the burst is observed WHILE the single background refresh is still in
     flight, which is what makes the <= 1 assertion deterministic rather than timing-dependent.

No socket/TestClient: main.health IS the registered handler (custom_route returns it
unchanged), exercised directly with a minimal fake Request, mirroring test_health_route.py.

Run: .venv\\Scripts\\python.exe -m pytest -q tests\\test_health_fast_under_cpu_load.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Same convention as test_health_route.py: pin a known bearer before importing main so we
# never depend on (or print) the real operator key.
os.environ.setdefault("MCP_API_KEY", "gwslow-test-bearer-key-do-not-use")

import main  # noqa: E402  (must follow the env setup above)


def _run(coro):
    """Drive a coroutine on a private loop WITHOUT waiting on executor stragglers at exit.
    asyncio.run() closes the loop and the interpreter later joins non-daemon pool workers; a
    probe still blocked would hang teardown. We only need the coroutine's own result here, and
    every test that leaves a probe blocked releases it in a finally before returning."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _warm_watched_cache() -> None:
    """Force the first (synchronous) compute so later calls are on the stale-while-revalidate
    path, then wait for any in-flight background refresh to settle."""
    main._watched_code_changed()
    for _ in range(300):
        with main._watched_refresh_lock:
            if not main._watched_refreshing["busy"]:
                break
        import time as _t
        _t.sleep(0.01)


def test_gather_health_probes_reports_degraded_and_honors_budget():
    """The core of the fix: when probes do not finish in the budget, the answer keeps the fast
    results, SAYS degraded, counts the stragglers, and returns without waiting for them.

    DETERMINISTIC, NOT TIMED: two blocker probes are each held open on a threading.Event that
    this test releases only AFTER the gather has returned. A probe that cannot return until we
    say so is guaranteed to miss ANY budget, so the degraded path is reached every single run
    regardless of how loaded the box is. The proof that the budget was honoured is not a
    wall-clock reading -- it is that the gather returned with the fast result present WHILE the
    two blockers were still blocked (they are only released afterwards). The health pool has
    four workers, so the one fast probe always gets a slot even with both blockers occupying
    theirs."""
    release = threading.Event()
    fast_started = threading.Event()
    blocker_started = threading.Barrier(3)  # two blockers + this thread

    def fast():
        fast_started.set()
        return {"fast_ran": True}

    def blocker():
        # Signal we are running (so the test can confirm both blockers occupied a worker),
        # then block until released -- stands in for a probe starved past the budget, with no
        # fixed sleep that could outlive (or undershoot) teardown.
        blocker_started.wait(timeout=10)
        release.wait(10)
        return {"blocker_ran": True}

    saved = main._HEALTH_PROBE_BUDGET_S
    # A tiny budget keeps the gather's own asyncio.wait from lingering; correctness does not
    # depend on its exact value because the blockers never return until we release them.
    main._HEALTH_PROBE_BUDGET_S = 0.05
    try:
        # Confirm both blockers actually entered the pool (occupied a worker) before the
        # gather returns -- otherwise 'pending == 2' could pass for the wrong reason.
        def _await_blockers():
            blocker_started.wait(timeout=10)

        watcher = threading.Thread(target=_await_blockers, daemon=True)
        watcher.start()
        out = _run(main._gather_health_probes([fast, blocker, blocker]))
        watcher.join(timeout=10)
    finally:
        release.set()  # let the two blocker workers exit so they never pin the pool
        main._HEALTH_PROBE_BUDGET_S = saved

    # Fast probe result survives even though two siblings were still blocked when we returned.
    assert fast_started.is_set(), "fast probe never ran"
    assert out.get("fast_ran") is True, f"fast probe result lost: {out!r}"
    # The reply SAYS degraded and HOW MANY did not finish -- the truthful busy signal.
    assert out.get("health_degraded") is True, f"degraded flag not set: {out!r}"
    assert out.get("health_probes_pending") == 2, f"wrong pending count: {out!r}"
    # The blockers had NOT returned when the gather answered: their field is absent. This is
    # the deterministic proof the gather did not wait for the stragglers -- it is a fact about
    # what the returned dict contains, not about how many seconds elapsed.
    assert "blocker_ran" not in out, f"gather waited for a blocked straggler: {out!r}"


def test_watched_code_changed_refreshes_off_thread_not_on_the_health_path(monkeypatch):
    """Once warm, a /health-path call must NOT run the watched-tree walk inline, and a burst of
    callers hitting a stale cache must start at most ONE background walk -- never one walk per
    caller (which would re-create the saturation this fix removes). These are facts about WHICH
    THREAD walks, independent of how slow the walk or the box is.

    DETERMINISTIC: the walk blocks on a threading.Event instead of sleeping, so the whole burst
    of callers is issued while the single background refresh is provably still in flight. We
    then release it and assert exactly one walk ran, on a background thread."""
    _warm_watched_cache()

    walk_calls = {"n": 0}
    walk_threads: set[int] = set()
    walk_lock = threading.Lock()
    walk_entered = threading.Event()
    release_walk = threading.Event()

    def _blocking_newer_than(_ts):
        with walk_lock:
            walk_calls["n"] += 1
            walk_threads.add(threading.get_ident())
        walk_entered.set()
        # Hold the walk open so the whole caller burst below happens WHILE it is in flight --
        # deterministic, unlike a fixed sleep that could finish before the burst is issued.
        release_walk.wait(10)
        return False

    import tools.deploy_freshness as df
    monkeypatch.setattr(df, "newer_than", _blocking_newer_than, raising=True)

    # Make the cache look stale so the next call triggers a refresh.
    main._watched_cache["at"] = 0.0

    caller_thread = threading.get_ident()
    try:
        # First call kicks the single background refresh; wait until the walk has actually
        # entered (and is now blocked) so the rest of the burst is guaranteed concurrent.
        first = main._watched_code_changed()
        assert walk_entered.wait(10), "background walk never started"
        results = [first] + [main._watched_code_changed() for _ in range(49)]
    finally:
        release_walk.set()

    # Every warm call returns the cached boolean (old value served during revalidate).
    assert all(r in (True, False) for r in results), f"non-bool from warm call: {results!r}"
    # The walk, if it ran, ran on a BACKGROUND thread -- never the caller's. This is the
    # property whose absence put the tree walk on the /health path on 2026-11-07.
    assert caller_thread not in walk_threads, "watched-tree walk ran on the /health caller thread"
    # Let the single background refresh finish, then assert the whole burst started <= 1 walk.
    for _ in range(300):
        with main._watched_refresh_lock:
            if not main._watched_refreshing["busy"]:
                break
        import time as _t
        _t.sleep(0.01)
    assert walk_calls["n"] <= 1, f"{walk_calls['n']} concurrent watched-tree walks (expected <= 1)"


def _call_health_once(peer_host: str = "127.0.0.1"):
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = peer_host
    req.headers = MagicMock()
    req.headers.get = lambda k, default="": ""
    return _run(main.health(req))


def test_health_route_answers_and_never_drops_under_load(monkeypatch):
    """End-to-end: the real /health handler always returns a JSONResponse with status ok, and
    when a sub-probe stalls it surfaces health_degraded rather than hanging on the probes.

    DETERMINISTIC, NOT TIMED: we inject one extra sub-probe that blocks on a threading.Event
    the test releases only after the handler returned. Because that probe cannot finish, the
    budget is ALWAYS exceeded and the degraded path is reached on this single call -- no CPU
    spinners, no five-poll loop hoping one of them happens to miss a wall-clock budget (that
    hope is exactly what flaked on a box whose spinners held the GIL). The assertion is that
    the handler RETURNED a degraded body while the probe was still blocked, which is the
    load-independent proof that /health stays responsive and truthful under a stalled probe."""
    _warm_watched_cache()
    release = threading.Event()
    blocker_entered = threading.Event()

    def _blocking_probe():
        blocker_entered.set()
        release.wait(10)
        return {"blocker_probe_ran": True}

    # Wrap _gather_health_probes so the handler runs its real probes PLUS our stalled one,
    # without changing the product code. The handler still awaits the real gather helper.
    real_gather = main._gather_health_probes

    async def _gather_with_blocker(probes):
        return await real_gather(list(probes) + [_blocking_probe])

    monkeypatch.setattr(main, "_gather_health_probes", _gather_with_blocker)

    saved = main._HEALTH_PROBE_BUDGET_S
    main._HEALTH_PROBE_BUDGET_S = 0.05  # force the degraded path to be reachable deterministically
    try:
        resp = _call_health_once()
        body = getattr(resp, "body", b"") or b""
        # The handler ALWAYS returns a JSONResponse with status ok -- never raises, never drops
        # the connection. This is the property whose absence made the fleet send-gate read a
        # starved server as 'down' on 2026-11-07.
        assert b'"status"' in body and b'"ok"' in body, f"bad /health body: {body!r}"
        # The stalled probe made the gather exceed its budget, so the handler RETURNED a
        # degraded body instead of blocking on the probe. Deterministic: the probe is still
        # blocked at this point (released only in the finally below), so a handler that waited
        # for it could not have returned here at all.
        assert b'"health_degraded"' in body, (
            f"handler did not surface health_degraded while a probe was stalled: {body!r}")
        # Confirm the stalled probe really did enter the pool and is still held -- i.e. the
        # degraded signal is because a probe was pending, not for some unrelated reason.
        assert blocker_entered.is_set(), "the injected stalled probe never ran"
        assert b'"blocker_probe_ran"' not in body, "handler waited for the stalled probe"
    finally:
        release.set()  # let the stalled probe worker exit so it never pins the pool
        main._HEALTH_PROBE_BUDGET_S = saved
