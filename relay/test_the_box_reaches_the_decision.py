# -*- coding: utf-8 -*-
"""file -> box -> DECISION, exercised end to end with the objects production uses.

THE HALF THAT WAS NAMED AND NOT PROVEN. relay/test_a_setting_the_operator_changed_reaches_a_
running_fleet.py exercises file -> box with `fleet_runner.build_settings_follower`, and says in
its own docstring that box -> decision is asserted from SOURCE because the admission loop lived
inside a 700-line `run()`. Saying so was right, and it is still an untested half: a source
assertion cannot catch a cap read from the wrong place, compared the wrong way round, or
compared against a stale copy.

`relay_fleet.admits_another_tab` now has a name for exactly that reason, so the decision can be
driven with numbers -- no browser, no clock, no worker. These tests then join the two halves:
write a settings file, poll the REAL follower, and ask the REAL predicate using the box the
follower just wrote into.

The disk and RAM knobs already had names (`disk_admission_ok`, `ram_target_cap`), so their
halves are pinned the same way at the bottom.
"""
from __future__ import annotations

import os

import pytest

from relay.fleet_runner import build_settings_follower
from relay.relay_fleet import admits_another_tab, disk_admission_ok, ram_target_cap


@pytest.fixture
def settings(tmp_path):
    return str(tmp_path / "settings.txt")


# THE SHARED WRITER, NOT A LOCAL ONE. My first version bumped from the file's own mtime
# and reproduced a failure this repository had already found, written up and fixed inside
# the sibling test module: one run in several, only when the two files ran together,
# because two writes in one tick land on the same stamp and `maxtabs=2` / `maxtabs=4` are
# the same SIZE. See conftest.write_settings.
from conftest import write_settings as _write


# ---- the decision itself ---------------------------------------------------------------------

def test_a_worker_that_fits_the_budget_is_admitted():
    assert admits_another_tab(active_open=1, projected_peak=1, tab_weight=1, cap=3) is True


def test_a_worker_that_would_exceed_the_budget_is_not():
    assert admits_another_tab(active_open=1, projected_peak=3, tab_weight=1, cap=3) is False


def test_exactly_filling_the_budget_is_allowed():
    """<= not <. Off by one here is a fleet that never reaches its own cap."""
    assert admits_another_tab(active_open=1, projected_peak=2, tab_weight=1, cap=3) is True


def test_a_socket_worker_weighs_nothing_and_still_obeys_the_budget():
    """tab_weight is 0 for a socket worker, so the sum cannot grow -- which is the documented
    reason the tab budget alone cannot bound a socket-first fleet. The predicate must not
    pretend otherwise; it answers about TABS."""
    assert admits_another_tab(active_open=5, projected_peak=5, tab_weight=0, cap=3) is False
    assert admits_another_tab(active_open=5, projected_peak=2, tab_weight=0, cap=3) is True


def test_with_nothing_open_one_worker_is_always_admitted():
    """THE ESCAPE HATCH, AND IT IS LOAD-BEARING. The RAM autoscale writes mc_box, and
    it can genuinely drive the cap to zero; without this the fleet would stop forever with work
    queued and nothing running."""
    assert admits_another_tab(active_open=0, projected_peak=0, tab_weight=1, cap=0) is True


def test_a_cap_of_zero_still_refuses_a_SECOND_worker():
    """The hatch is for the first one only. If it applied with something already open, the cap
    would mean nothing at its most important value."""
    assert admits_another_tab(active_open=1, projected_peak=1, tab_weight=1, cap=0) is False


# ---- and the box the operator moved is the one the decision reads ------------------------------

def test_raising_maxtabs_in_the_file_admits_a_worker_the_old_cap_refused(settings):
    """THE JOIN. Same follower production builds, same box object, same predicate."""
    disk_box, ram_box, mc_box, asc_box = [6.0], [512.0], [2], [False, 100]
    _write(settings, disk_floor_gb=6, ram_floor_mb=512, maxtabs=2)
    f = build_settings_follower(disk_box, ram_box, mc_box, asc_box, path_fn=lambda: settings)

    # Two tabs open, cap 2: full.
    assert admits_another_tab(2, 2, 1, mc_box[0]) is False

    _write(settings, disk_floor_gb=6, ram_floor_mb=512, maxtabs=4)
    f.poll()
    assert mc_box[0] == 4, "the follower did not move the box"
    assert admits_another_tab(2, 2, 1, mc_box[0]) is True, \
        "the box moved and the decision did not follow it"


def test_lowering_maxtabs_stops_admitting_without_a_restart(settings):
    """The direction that matters operationally: an operator turning it DOWN mid-run."""
    disk_box, ram_box, mc_box, asc_box = [6.0], [512.0], [6], [False, 100]
    _write(settings, disk_floor_gb=6, ram_floor_mb=512, maxtabs=6)
    f = build_settings_follower(disk_box, ram_box, mc_box, asc_box, path_fn=lambda: settings)
    assert admits_another_tab(3, 3, 1, mc_box[0]) is True

    _write(settings, disk_floor_gb=6, ram_floor_mb=512, maxtabs=3)
    f.poll()
    assert mc_box[0] == 3
    assert admits_another_tab(3, 3, 1, mc_box[0]) is False


# ---- the other two knobs, whose decisions already had names -----------------------------------

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


def test_the_ram_floor_the_operator_set_reaches_the_autoscale(settings):
    """ram_box feeds ram_target_cap's headroom. A bigger floor reserves more, so the cap the
    autoscale is willing to hold cannot be larger than with a smaller floor."""
    disk_box, ram_box, mc_box, asc_box = [6.0], [512.0], [3], [True, 100]
    _write(settings, disk_floor_gb=6, ram_floor_mb=512, maxtabs=3)
    f = build_settings_follower(disk_box, ram_box, mc_box, asc_box, path_fn=lambda: settings)
    low = ram_target_cap(1, 3, 100, per_tab_mb=500, headroom_mb=ram_box[0], up_margin_mb=0)

    _write(settings, disk_floor_gb=6, ram_floor_mb=32768, maxtabs=3)
    f.poll()
    assert ram_box[0] == 32768.0, "the follower did not move the RAM box"
    high = ram_target_cap(1, 3, 100, per_tab_mb=500, headroom_mb=ram_box[0], up_margin_mb=0)
    assert high <= low, \
        "reserving 32 GB allowed at least as many tabs as reserving 512 MB (%r vs %r)" % (high, low)


# ---- the one link that is still source, said out loud ------------------------------------------

def test_the_admission_loop_asks_with_the_box_and_not_a_copy():
    """WHAT THE TESTS ABOVE DO NOT PROVE.

    They pass `mc_box[0]` to the predicate themselves, so they show the box and the decision
    agree -- not that `run()` is the one passing it. That call still lives inside a 700-line
    function with a browser and a clock in scope, so it is asserted from source, exactly as the
    sibling file does for `.watch("maxtabs")`.

    The difference from before is the SIZE of what is assumed: it was a four-line inline
    comparison buried in the loop, and it is now one named call on one line. An assumption you
    can see in one line is a different risk from one spread over four, and naming it here means
    the next person does not have to rediscover which half is which.
    """
    from conftest import code_only

    here = os.path.dirname(os.path.abspath(__file__))
    src = code_only(os.path.join(here, "relay_fleet.py"))
    assert "admits_another_tab(" in src, "the loop stopped asking the named predicate"
    i = src.index("while pending and admits_another_tab(")
    call = src[i:i + 300]
    assert "mc_box[0]" in call, \
        "the admission loop is no longer passing the live box as the cap"
