"""Tests for the watchdog's PRE-WEDGE recycle decision
(fleet_runner._watchdog_should_preempt_recycle).

The two fleet-Edge losses on 2026-11-09 (21:19:56 "Edge context lost, resuming 43 goals" and
22:30-22:37 "watchdog: status frozen 296s, browser does not answer") were preceded by minutes
of memory thrash under a STILL-RESPONSIVE DevTools port: at 21:42 free RAM was 778 MB with ~48
msedge processes and sweep laps of 47-166s (baseline 6-17s). _watchdog_should_reset() only ever
resets an Edge that is ALREADY wedged; for a browser that still answers its port it WAITS, with
no view of memory, so the thrash was allowed to deepen into a wedge every time.

_watchdog_should_preempt_recycle fills that gap: while the watchdog is NOT resetting, it recycles
the companion Edge the moment memory crosses the SAME measured thresholds the pre-run recycle
uses (edge_recover.should_recycle: cap 1500 MB, floor 1000 MB). These tests pin that behaviour
and the guards that stop it firing at the wrong time.

Run:  .venv\\Scripts\\python.exe relay\\test_watchdog_preemptive_recycle.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from relay.fleet_runner import _watchdog_should_preempt_recycle
from relay.edge_recover import RECYCLE_EDGE_CAP_MB, RECYCLE_FREE_FLOOR_MB

results = []


def check(name, cond):
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


def main():
    # --- the measured 2026-11-09 pre-loss reading: 778 MB free, responsive port, no recovery ---
    # free RAM 778 < 1000 floor -> recycle BEFORE it wedges. This is the case the old watchdog
    # slept through.
    recyc, why = _watchdog_should_preempt_recycle(
        edge_mb=0.0, free_mb=778.0, cdp_responsive=True, recovering=False)
    check("the_778mb_prelude_recycles", recyc and "pre-wedge" in why and "free RAM" in why)

    # --- the healthy 22:38 restart had 2506 MB free -> leave it alone ---
    recyc, why = _watchdog_should_preempt_recycle(
        edge_mb=900.0, free_mb=2506.0, cdp_responsive=True, recovering=False)
    check("a_healthy_browser_is_left_alone", recyc is False and "within memory limits" in why)

    # --- a bloated Edge (> 1500 cap) with healthy free RAM still recycles ---
    recyc, why = _watchdog_should_preempt_recycle(
        edge_mb=1800.0, free_mb=4000.0, cdp_responsive=True, recovering=False)
    check("a_bloated_edge_recycles", recyc and "pre-wedge" in why and "Edge" in why)

    # --- GUARD: an unresponsive port is a REAL wedge, not a pre-wedge -> defer to the reset
    #     path, do NOT pre-empt (even though 778 MB is below the floor) ---
    recyc, why = _watchdog_should_preempt_recycle(
        edge_mb=0.0, free_mb=778.0, cdp_responsive=False, recovering=False)
    check("a_dead_port_is_left_to_the_reset_path", recyc is False and "not answering" in why)

    # --- GUARD: a recovery already in flight -> do not start a second one ---
    recyc, why = _watchdog_should_preempt_recycle(
        edge_mb=0.0, free_mb=778.0, cdp_responsive=True, recovering=True)
    check("a_recovery_in_flight_is_not_doubled", recyc is False and "in flight" in why)

    # --- no memory signal (unknown edge size 0, healthy free RAM) -> nothing to pre-empt ---
    recyc, why = _watchdog_should_preempt_recycle(
        edge_mb=0.0, free_mb=4000.0, cdp_responsive=True, recovering=False)
    check("no_signal_no_recycle", recyc is False)

    # --- cdp_responsive=None (probe inconclusive) is treated as still-alive: only an explicit
    #     False means wedged. With free RAM below the floor it still pre-empts. ---
    recyc, why = _watchdog_should_preempt_recycle(
        edge_mb=0.0, free_mb=778.0, cdp_responsive=None, recovering=False)
    check("an_inconclusive_probe_still_pre_empts_on_low_ram", recyc is True)

    # --- the thresholds are the pre-run recycle's own, not a second copy that can drift ---
    check("thresholds_are_the_measured_floors",
          RECYCLE_EDGE_CAP_MB == 1500.0 and RECYCLE_FREE_FLOOR_MB == 1000.0)

    # --- explicit per-call thresholds are honoured (lets the live fleet tune) ---
    recyc, _ = _watchdog_should_preempt_recycle(
        edge_mb=900.0, free_mb=4000.0, cdp_responsive=True, recovering=False, edge_cap_mb=800.0)
    check("an_explicit_cap_is_honoured", recyc is True)

    print("\n=== %d/%d pre-wedge recycle checks passed ===" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
