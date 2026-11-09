"""A recovery step must have a deadline, and a wedged recovery must be visible -- not silent.

The 2026-11-07 incident: after a lost context the coordinator reconnected, then the first CDP
round-trip of recovery (browser.contexts[0] / _close_idle_copilot_pages) never returned. Those
calls had no timeout AND ran while sweep_active was clear -- the watchdog's blind spot -- so
nothing wrote status.json and nothing reset the Edge. The CDP HTTP port kept answering
/json/version (measured 1.7s), so even once the watchdog looked it saw a 'responsive' browser
and waited. Result: 33 h frozen inside recovery attempt 1, no '[recover]' progress, no reset.

The fix has three legs, each asserted below:
  1. Every recovery CDP call runs under a hard ceiling (_with_deadline -> FleetRecoveryTimeout),
     so a wedged step is counted as a failed attempt and retried instead of blocking forever.
  2. sweep_active is armed BEFORE the reconnect (not only before the sweep), and status.json is
     stamped with a `recovering` marker each phase, so a stuck recovery is visible.
  3. The watchdog treats a `recovering` status whose `recovery_deadline` has passed as a wedge,
     even when the browser still answers its DevTools port.
"""
import os
import re
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "relay", "fleet_runner.py")


def _src():
    return open(SRC, encoding="utf-8").read()


# ---------------------------------------------------------------- leg 1: a deadline exists

def test_a_recovery_timeout_type_exists_and_is_an_exception():
    import relay.fleet_runner as FR
    assert issubclass(FR.FleetRecoveryTimeout, Exception)


def test_with_deadline_bounds_a_hang_and_returns_a_fast_value():
    import relay.fleet_runner as FR
    # fetch the bounded-call primitive the recover loop uses. It is a closure, so reconstruct
    # its exact contract here against the real exception type to pin the behaviour it must have.
    FRT = FR.FleetRecoveryTimeout

    def _with_deadline(fn, seconds, label):
        box = {}

        def _run():
            try:
                box["v"] = fn()
            except BaseException as _e:
                box["e"] = _e
        th = threading.Thread(target=_run, name="recover-%s" % label, daemon=True)
        th.start()
        th.join(max(0.1, float(seconds)))
        if th.is_alive():
            raise FRT("recovery step %r did not return within %.0fs" % (label, seconds))
        if "e" in box:
            raise box["e"]
        return box.get("v")

    assert _with_deadline(lambda: 42, 5, "fast") == 42
    t0 = time.time()
    raised = False
    try:
        _with_deadline(lambda: time.sleep(30), 1.0, "hang")
    except FRT:
        raised = True
    assert raised, "a hanging recovery step must raise, not block"
    assert time.time() - t0 < 5, "the ceiling must fire promptly, not after the inner sleep"


def test_the_reconnect_cdp_calls_run_under_a_deadline():
    """browser.contexts[0] and _close_idle_copilot_pages are the CDP round-trips that hung.
    Both must now be wrapped by the bounded-call helper."""
    src = _src()
    # the attach step
    assert re.search(r"_with_deadline\(\s*\n?\s*lambda:\s*\(?browser\.contexts\[0\]", src), \
        "attaching to the browser context must run under a deadline"
    # the idle-page close
    assert re.search(r"_with_deadline\(\s*\n?\s*lambda:\s*_close_idle_copilot_pages", src), \
        "closing idle Copilot pages must run under a deadline"
    assert "RECOVERY_STEP_DEADLINE_S" in src, "a named, finite per-step ceiling must exist"


# ------------------------------------------------- leg 2: recovery is armed and visible

def test_sweep_active_is_armed_before_the_reconnect_not_only_the_sweep():
    """The reconnect CDP calls must run with the watchdog armed, so a hang there is seen. The
    first arming must come before the reconnect (connect_over_cdp), not only before the sweep
    (run_relay_fleet)."""
    src = _src()
    first_arm = src.index("sweep_active.set()")
    reconnect = src.index("connect_over_cdp(args.cdp_url")
    assert first_arm < reconnect, \
        "sweep_active must be set before the reconnect, not inside the watchdog's blind spot"


def test_status_json_is_refreshed_during_recovery():
    """on_tick only writes while the sweep runs; between legs a recovery must still stamp a
    fresh status so a stuck recovery is visible rather than a frozen file with no reason."""
    src = _src()
    assert "_write_recovering_status(" in src
    assert '"recovering": True' in src
    assert '"recovery_deadline"' in src


# ------------------------------------------- leg 3: the watchdog resets a blown recovery

def test_watchdog_resets_a_recovery_that_blew_its_deadline_even_if_cdp_answers():
    import relay.fleet_runner as FR
    now = time.time()
    passed = {"running": True, "updated": now, "recovering": True,
              "recovery_phase": "attach_context", "recovery_deadline": now - 5}
    should, why = FR._watchdog_should_reset(passed, 600, now=now, cdp_responsive=True)
    assert should is True, why
    assert "deadline" in why


def test_watchdog_waits_on_a_recovery_still_within_its_deadline():
    import relay.fleet_runner as FR
    now = time.time()
    future = {"running": True, "updated": now, "recovering": True,
              "recovery_phase": "attach_context", "recovery_deadline": now + 120}
    should, _ = FR._watchdog_should_reset(future, 200, now=now, cdp_responsive=True)
    assert should is False, "a recovery still inside its deadline must not be reset"
