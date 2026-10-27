# -*- coding: utf-8 -*-
"""file -> box -> DECISION, exercised end to end with the objects production uses.

THE HALF THAT WAS NAMED AND NOT PROVEN. relay/test_a_setting_the_operator_changed_reaches_a_
running_fleet.py exercises file -> box with `fleet_runner.build_settings_follower`, and says in
its own docstring that box -> decision is asserted from SOURCE because the admission loop lived
inside a 700-line `run()`. Saying so was right, and it is still an untested half: a source
assertion cannot catch a cap read from the wrong place, compared the wrong way round, or
compared against a stale copy.

The tab-budget predicate (`admits_another_tab`) and the RAM autoscale (`ram_target_cap`) were
retired with the tab era; the live box now reaches the decision through the socket capacity
cap, pinned from source at the bottom. The disk knob still has a name (`disk_admission_ok`),
so its half is pinned by driving the REAL follower and the REAL predicate.
"""
from __future__ import annotations

import os

import pytest

from relay.fleet_runner import build_settings_follower
from relay.relay_fleet import disk_admission_ok


@pytest.fixture
def settings(tmp_path):
    return str(tmp_path / "settings.txt")


# THE SHARED WRITER, NOT A LOCAL ONE. My first version bumped from the file's own mtime
# and reproduced a failure this repository had already found, written up and fixed inside
# the sibling test module: one run in several, only when the two files ran together,
# because two writes in one tick land on the same stamp and `maxtabs=2` / `maxtabs=4` are
# the same SIZE. See conftest.write_settings.
from conftest import write_settings as _write


# ---- the disk knob, whose decision already had a name ------------------------------------------

def test_the_disk_floor_the_operator_set_is_the_one_the_gate_uses(settings):
    disk_box, ram_box, mc_box, asc_box = [6.0], [512.0], [3], [False, 100]
    _write(settings, disk_floor_gb=6, ram_floor_mb=512, maxtabs=3)
    f = build_settings_follower(disk_box, ram_box, mc_box, asc_box, path_fn=lambda: settings)

    # 10 GB free, 1 GB eval: fine under a 6 GB floor, refused under a 20 GB one.
    assert disk_admission_ok(floor_gb=disk_box[0], eval_gb=1.0, free_gb=10.0) is True
    _write(settings, disk_floor_gb=20, ram_floor_mb=512, maxtabs=3)
    f.poll()
    assert disk_box[0] == 20.0
    assert disk_admission_ok(floor_gb=disk_box[0], eval_gb=1.0, free_gb=10.0) is False


# ---- the one link that is still source, said out loud ------------------------------------------

def test_the_admission_loop_asks_with_the_box_and_not_a_copy():
    """The live lane box (`mc_box[0]`, or the autoscale ceiling) reaches the admission decision
    through the socket capacity cap, which is the RAM safeguard applied to it."""
    from conftest import code_only

    here = os.path.dirname(os.path.abspath(__file__))
    src = code_only(os.path.join(here, "relay_fleet.py"))
    i = src.index("_lane_ceiling = ")
    block = src[i:i + 900]
    assert "mc_box[0]" in block, "the live box is no longer the lane ceiling"
    assert "socket_capacity.ram_safeguard_cap(" in block
    j = src.index("while pending:")
    assert "_socket_cap = min(_lane_ceiling, socket_capacity.ram_safeguard_cap(" in block
    assert "concurrency_admits(workers, _socket_cap," in src[j:j + 3000]
    helper = src[src.index("def concurrency_admits("):src.index("def _holds_slot(")]
    assert "limit = max(1, cap) + (extra if priority else 0)" in helper
    assert "limit = min(limit, ram_cap)" in helper
    assert "socket_capacity.admits_another_goal(active_worker_count(workers), limit)" in helper
