# -*- coding: utf-8 -*-
"""A preemptive tab ceiling that fires BEFORE the wedge, not after it.

The wedge of 2026-11-09: the coordinator log recorded free RAM 778 MB with 48 msedge
processes and CPU 89% at 21:42, moments before the watchdog declared Edge wedged (status
frozen, DevTools port unanswered) and hard-reset it, losing 43 then 30 goals across two
recoveries. The fix adds `_preemptive_tab_ceiling`, which stops admitting new tabs while
there is still headroom to drain -- RAM floor 1024 MB (ABOVE the observed 778 MB failure
point) and process cap 40 (BELOW the observed 48). This suite pins that the ceiling arms
before the measured failure point, never below the already-open count, and that each arm
can be disabled independently without disabling the other.
"""
from __future__ import annotations

import os  # noqa: E402

import pytest  # noqa: E402

from relay import fleet_runner as FR  # noqa: E402


HUGE = 100000  # a ceiling this high means "no ceiling": admission is not throttled


def test_constants_bracket_the_observed_wedge():
    # floor must sit ABOVE the 778 MB seen at failure; cap must sit BELOW the 48 procs seen.
    assert FR.EDGE_WEDGE_RAM_FLOOR_MB >= 778
    assert FR.EDGE_WEDGE_PROC_CAP <= 48


def test_wedge_conditions_hold_admission():
    # at the exact wedge reading (778 MB / 48 procs) the ceiling must clamp to the open count.
    assert FR._preemptive_tab_ceiling(20, 778, 48) == 20


def test_low_ram_alone_holds():
    assert FR._preemptive_tab_ceiling(20, FR.EDGE_WEDGE_RAM_FLOOR_MB, 1) == 20


def test_high_proc_alone_holds():
    assert FR._preemptive_tab_ceiling(20, 16000, FR.EDGE_WEDGE_PROC_CAP) == 20


def test_ceiling_never_dips_below_already_open():
    # 37 tabs already open, pressure present: do not ask to close below what is open.
    assert FR._preemptive_tab_ceiling(37, 100, 60) == 37


def test_headroom_imposes_no_ceiling():
    assert FR._preemptive_tab_ceiling(20, 8000, 5) > HUGE


def test_missing_ram_reading_skips_ram_arm():
    assert FR._preemptive_tab_ceiling(20, None, 5) > HUGE


def test_missing_proc_reading_skips_proc_arm():
    assert FR._preemptive_tab_ceiling(20, 8000, None) > HUGE


def test_proc_cap_zero_disables_proc_arm():
    # proc_cap=0 turns the process arm OFF entirely, even at an absurd process count.
    assert FR._preemptive_tab_ceiling(20, 8000, 999, proc_cap=0) > HUGE


def test_proc_cap_zero_leaves_ram_arm_live():
    # disabling the proc arm must not disable the ram arm.
    assert FR._preemptive_tab_ceiling(20, 100, 999, proc_cap=0) == 20


def test_unparseable_open_count_floors_to_zero():
    assert FR._preemptive_tab_ceiling("nonsense", 100, 1) == 0


def test_nonnumeric_readings_skip_both_arms():
    assert FR._preemptive_tab_ceiling(20, "x", "y") > HUGE


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
