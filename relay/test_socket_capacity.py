from __future__ import annotations

import os

from relay import socket_capacity as SC
from tools import settings_keys as SK


def test_ram_never_drains_a_running_goal():
    # free RAM far below the floor: the cap is still the running count, never lower
    assert SC.ram_safeguard_cap(40, avail_mb=100, floor_mb=512, cost_mb=25, ceiling=200) == 40
    assert SC.admits_another_goal(40, 40) is False       # holds new admissions only


def test_nothing_running_always_admits_one():
    cap = SC.ram_safeguard_cap(0, avail_mb=0, floor_mb=512, cost_mb=25, ceiling=200)
    assert cap == 1 and SC.admits_another_goal(0, cap)


def test_headroom_arithmetic_is_priced_per_goal_and_bounded_by_the_ceiling():
    # 1372 MB free, 512 floor, 25 MB/goal -> 34 more
    assert SC.headroom_goals(1372, 512, 25) == 34
    assert SC.ram_safeguard_cap(5, 1372, 512, 25, 200) == 39
    assert SC.ram_safeguard_cap(5, 10 ** 6, 512, 25, 200) == 200      # ceiling wins
    assert SC.ram_safeguard_cap(250, 10 ** 6, 512, 25, 200) == 250    # never below running


def test_goal_cost_is_declared_settable_and_positive(monkeypatch):
    assert SC.GOAL_MB_DEFAULT == SK.default("socket_goal_mb") == 25.0
    import relay.fleet_runner as FR
    monkeypatch.setattr(FR, "_settings_float", lambda key, d: 40.0)
    assert SC.goal_cost_mb() == 40.0
    monkeypatch.setattr(FR, "_settings_float", lambda key, d: -1.0)
    assert SC.goal_cost_mb() == SC.GOAL_MB_DEFAULT


def test_lane_ceiling_uses_operational_limit_and_rate_estimate():
    assert SK.derive_lane_ceiling(100, 60, 2) == 200
    assert SK.LANE_CEILING_DEFAULT == 50
    assert SK.derive_lane_ceiling(50, 60, 2) == 100            # follows the rate
    from relay.quota_meter import LIMIT_RPM
    assert SK.CLOUD_RPM == LIMIT_RPM
    import relay.fleet_runner as FR
    assert FR.LANES_DEFAULT == FR.DEFAULT_MAX_CONCURRENT == FR.AUTOSCALE_CEILING_DEFAULT == 50


def test_the_word_emergency_is_gone_from_code():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    hits = []
    for sub in ("relay", "tools", "ui"):
        for dp, _d, files in os.walk(os.path.join(root, sub)):
            for f in files:
                if f.endswith((".py", ".cs")) and f != "test_socket_capacity.py":
                    with open(os.path.join(dp, f), encoding="utf-8-sig", errors="replace") as fh:
                        body = fh.read()
                        if "EMERGENCY" in body or "emergency override active" in body:
                            hits.append(f)
    assert not hits, hits


def test_send_bucket_rate_is_min_of_cloud_and_ceiling(monkeypatch):
    from relay import relay_fleet as RF
    RF._reset_admission_pacing()
    monkeypatch.setattr(RF, "rate_ceiling", lambda default=None: 40.0)
    assert RF.socket_send_rate_now(0.0) == (40.0, 8.0)
    monkeypatch.setattr(RF, "rate_ceiling", lambda default=None: 500.0)
    assert RF.socket_send_rate_now(0.0) == (100.0, 8.0)
