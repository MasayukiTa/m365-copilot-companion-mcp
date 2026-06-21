"""Unit tests for the L2 cron entrypoint. Run: python -m relay.selfimprove.test_l2_cron

Hermetic: NEVER calls the real l2.run_iteration (which would drive the fleet), NEVER runs git, no
network, no real scheduler registration. The iteration is replaced by a stub iterate_fn that records
its calls; frozen_intact is monkeypatched so the frozen-set check is deterministic; the lock and
baseline live in temp files; pid-liveness is injected (is_alive_fn).
"""
import os
import tempfile

from relay.selfimprove import l2_cron as C
from relay.selfimprove import frozen as F


# --------------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------------

def _patch_frozen(monkey_ok, changed=None):
    """Replace F.frozen_intact (the symbol l2_cron calls through) with a fixed result; return restorer."""
    orig = F.frozen_intact
    F.frozen_intact = lambda *a, **k: (monkey_ok, changed or [])
    return orig


class _StubIterate:
    """A stand-in for l2.run_iteration that records calls and returns a canned result dict."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else {"status": "queued", "final_keep": True}

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


# --------------------------------------------------------------------------------------------------
# (a) IterationLock: live lock blocks; stale lock is reclaimable
# --------------------------------------------------------------------------------------------------

def test_lock_live_blocks_and_stale_reclaims():
    with tempfile.TemporaryDirectory() as d:
        lp = os.path.join(d, "l2_cron.lock")

        # first holder (pid 111) takes the lock
        holder = C.IterationLock(lp, is_alive_fn=lambda pid: True, pid=111)
        assert holder.acquire() is True
        assert os.path.isfile(lp)

        # a SECOND acquire (pid 222) while the holder's pid is LIVE -> denied
        live_other = C.IterationLock(lp, is_alive_fn=lambda pid: True, pid=222)
        assert live_other.acquire() is False

        # but if the holder's pid is DEAD (stale lock), pid 222 reclaims it
        stale_other = C.IterationLock(lp, is_alive_fn=lambda pid: False, pid=222)
        assert stale_other.acquire() is True
        # the lock file now records the reclaiming pid
        with open(lp, encoding="utf-8") as f:
            assert f.read().strip() == "222"

        # release removes the file (owned by 222)
        stale_other.release()
        assert not os.path.isfile(lp)
    print("ok test_lock_live_blocks_and_stale_reclaims")


def test_lock_context_manager():
    with tempfile.TemporaryDirectory() as d:
        lp = os.path.join(d, "l2_cron.lock")
        with C.IterationLock(lp, is_alive_fn=lambda pid: True, pid=111) as lk:
            assert lk.acquired is True
            assert os.path.isfile(lp)
        assert not os.path.isfile(lp)   # released on exit
    print("ok test_lock_context_manager")


# --------------------------------------------------------------------------------------------------
# (b) run_once dry_run=True: gates pass, iterate_fn NOT called
# --------------------------------------------------------------------------------------------------

def test_run_once_dry_run_does_not_iterate():
    orig = _patch_frozen(True)
    try:
        with tempfile.TemporaryDirectory() as d:
            lp = os.path.join(d, "l2_cron.lock")
            stub = _StubIterate()
            res = C.run_once(dry_run=True, lock_path=lp, iterate_fn=stub)
            assert res["status"] == "dry_run", res
            assert res["frozen_ok"] is True
            assert stub.calls == []                       # the iteration was NOT run
            assert not os.path.isfile(lp)                 # lock released
    finally:
        F.frozen_intact = orig
    print("ok test_run_once_dry_run_does_not_iterate")


# --------------------------------------------------------------------------------------------------
# (c) run_once frozen changed -> abort, iterate NOT called
# --------------------------------------------------------------------------------------------------

def test_run_once_frozen_abort():
    orig = _patch_frozen(False, ["relay/selfimprove/guards.py"])
    try:
        with tempfile.TemporaryDirectory() as d:
            lp = os.path.join(d, "l2_cron.lock")
            stub = _StubIterate()
            res = C.run_once(dry_run=False, lock_path=lp, iterate_fn=stub)
            assert res["status"] == "abort", res
            assert "frozen set changed" in res["reason"]
            assert "guards.py" in res["reason"]
            assert res["frozen_ok"] is False
            assert stub.calls == []                       # never iterated
            assert not os.path.isfile(lp)                 # lock released
    finally:
        F.frozen_intact = orig
    print("ok test_run_once_frozen_abort")


# --------------------------------------------------------------------------------------------------
# (d) run_once spend ceiling exceeded -> ceiling, iterate NOT called
# --------------------------------------------------------------------------------------------------

def test_run_once_ceiling():
    from relay.selfimprove import l2 as L2
    orig = _patch_frozen(True)
    try:
        with tempfile.TemporaryDirectory() as d:
            lp = os.path.join(d, "l2_cron.lock")
            stub = _StubIterate()
            sc = L2.SpendCeiling(start_ts=1000.0, iters=3)
            res = C.run_once(dry_run=False, lock_path=lp, iterate_fn=stub,
                             spend=sc, max_iters=3, max_hours=None,
                             now_fn=lambda: 1000.0)
            assert res["status"] == "ceiling", res         # 3 >= 3 (inclusive)
            assert res["frozen_ok"] is True
            assert stub.calls == []                        # never iterated
            assert not os.path.isfile(lp)                  # lock released
    finally:
        F.frozen_intact = orig
    print("ok test_run_once_ceiling")


# --------------------------------------------------------------------------------------------------
# (e) run_once real path: gates pass -> ran, stub iterate called once, result returned
# --------------------------------------------------------------------------------------------------

def test_run_once_real_runs_stub_once():
    from relay.selfimprove import l2 as L2
    orig = _patch_frozen(True)
    try:
        with tempfile.TemporaryDirectory() as d:
            lp = os.path.join(d, "l2_cron.lock")
            stub = _StubIterate(result={"status": "queued", "final_keep": True, "marker": "Z"})
            sc = L2.SpendCeiling(start_ts=0.0)
            res = C.run_once(dry_run=False, lock_path=lp, iterate_fn=stub,
                             toggle="SWE_TEST_KNOB", n=200, dataset_key="Verified",
                             spend=sc, now_fn=lambda: 0.0)
            assert res["status"] == "ran", res
            assert res["result"]["marker"] == "Z"          # the stub's result is returned verbatim
            assert len(stub.calls) == 1                     # iterated exactly once
            kw = stub.calls[0]
            assert kw["toggle"] == "SWE_TEST_KNOB" and kw["n"] == 200
            assert kw["dataset_key"] == "Verified" and kw["auto_commit"] is False
            assert sc.iters == 1                            # ceiling ticked
            assert not os.path.isfile(lp)                   # lock released
    finally:
        F.frozen_intact = orig
    print("ok test_run_once_real_runs_stub_once")


# --------------------------------------------------------------------------------------------------
# (f) run_once while the lock is already held by a live iteration -> skipped
# --------------------------------------------------------------------------------------------------

def test_run_once_skipped_when_locked():
    orig = _patch_frozen(True)
    try:
        with tempfile.TemporaryDirectory() as d:
            lp = os.path.join(d, "l2_cron.lock")
            # a live foreign holder occupies the lock
            holder = C.IterationLock(lp, is_alive_fn=lambda pid: True, pid=999999)
            assert holder.acquire() is True
            stub = _StubIterate()
            # run_once sees a LIVE lock (inject is_alive_fn -> True) -> skipped, nothing run
            res = C.run_once(dry_run=False, lock_path=lp, iterate_fn=stub,
                             is_alive_fn=lambda pid: True)
            assert res["status"] == "skipped", res
            assert "another iteration is running" in res["reason"]
            assert stub.calls == []
            # the foreign holder's lock is untouched (still present)
            assert os.path.isfile(lp)
            holder.release()
    finally:
        F.frozen_intact = orig
    print("ok test_run_once_skipped_when_locked")


# --------------------------------------------------------------------------------------------------
# (g) cron_command returns the scheduler string
# --------------------------------------------------------------------------------------------------

def test_cron_command():
    s = C.cron_command()
    assert isinstance(s, str)
    assert "-m relay.selfimprove.l2_cron" in s
    assert "--run" in s                                    # default form is the real-run command
    assert "python.exe" in s
    # dry-run form
    sd = C.cron_command(dry_run=True)
    assert "--dry-run" in sd and "-m relay.selfimprove.l2_cron" in sd
    print("ok test_cron_command")


# --------------------------------------------------------------------------------------------------
# (h) run_once never raises: an exploding iterate_fn -> status error, lock released
# --------------------------------------------------------------------------------------------------

def test_run_once_never_raises():
    orig = _patch_frozen(True)
    try:
        with tempfile.TemporaryDirectory() as d:
            lp = os.path.join(d, "l2_cron.lock")

            def boom(**kwargs):
                raise RuntimeError("kaboom")

            res = C.run_once(dry_run=False, lock_path=lp, iterate_fn=boom)
            assert res["status"] == "error", res
            assert "kaboom" in res["reason"]
            assert not os.path.isfile(lp)                  # lock still released on error
    finally:
        F.frozen_intact = orig
    print("ok test_run_once_never_raises")


if __name__ == "__main__":
    test_lock_live_blocks_and_stale_reclaims()
    test_lock_context_manager()
    test_run_once_dry_run_does_not_iterate()
    test_run_once_frozen_abort()
    test_run_once_ceiling()
    test_run_once_real_runs_stub_once()
    test_run_once_skipped_when_locked()
    test_cron_command()
    test_run_once_never_raises()
    print("ALL L2_CRON TESTS PASSED")
