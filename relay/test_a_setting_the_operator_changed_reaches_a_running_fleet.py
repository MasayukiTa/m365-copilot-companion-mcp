# -*- coding: utf-8 -*-
"""A knob moved in the settings panel must change the run that is already going.

On 2026-09-15 a coordinator started at 15:08:26 and the operator saved their
settings at 15:15. For the rest of that run the disk floor was 4 GB and the RAM
floor 1024 MB, while the panel in front of the operator said 1 GB and 512 MB. The
resolution chain (CLI > settings.txt > env) was implemented correctly and the
autostart path had already been fixed to stop passing a flag that overrode the
file. The chain was simply evaluated once, at launch, into a one-element list --
and a number read once from a file a person keeps editing starts drifting the
moment they edit it.

That BOTH floors were wrong, by different amounts, is what identified it as the
mechanism rather than a bad value: two independent knobs do not disagree with the
same file in the same direction by accident.

These tests pin the behaviour that replaces it, including the half that is easy to
lose -- that following the file must not stamp on a live override. The cockpit's
強制開始 drops the disk floor to 0 so a run can start on a full disk; a follower
that re-asserted the file's value would undo the operator's decision a second after
they made it. Following CHANGES rather than VALUES is what makes both true at once.
"""
import os

import pytest

from relay.settings_follow import Follower


#: The last mtime this helper handed out. MONOTONIC BY CONSTRUCTION, which the real clock is
#: not at this resolution.
_LAST_FORCED_MTIME = [0.0]


def _write(path, **pairs):
    with open(path, "w", encoding="utf-8") as fh:
        for k, v in pairs.items():
            fh.write("%s=%s\n" % (k, v))
    # The follower re-reads on (mtime, size). Tests write the same file repeatedly and can land
    # inside one filesystem timestamp tick, which would make a real change look unchanged.
    #
    # BUMPING FROM THE FILE'S OWN MTIME WAS NOT ENOUGH, and produced a failure that appeared
    # roughly one run in several and could not be reproduced on demand. Two writes in the same
    # tick both yield stamp = T + 10, and `disk_floor_gb=1` and `disk_floor_gb=3` are the same
    # SIZE -- so the cache key matched exactly and a real change was reported as none. Whether
    # the two writes share a tick depends on whatever ran before them, which is why the same
    # test passed alone and failed in company.
    st = os.stat(path)
    forced = max(st.st_mtime, _LAST_FORCED_MTIME[0]) + 10
    _LAST_FORCED_MTIME[0] = forced
    os.utime(path, (st.st_atime, forced))


@pytest.fixture()
def settings(tmp_path):
    return str(tmp_path / "settings.txt")


def test_a_change_to_the_file_reaches_the_live_value(settings):
    _write(settings, disk_floor_gb=4)
    box = [4.0]
    f = Follower(lambda: settings).watch("disk_floor_gb", lambda v: box.__setitem__(0, v))
    f.prime()

    assert f.poll() == []                      # nothing changed yet
    assert box[0] == 4.0

    _write(settings, disk_floor_gb=1)          # the operator moves the knob
    assert f.poll() == [("disk_floor_gb", 1.0)]
    assert box[0] == 1.0, "the run kept using the floor it was launched with"


def test_a_run_nobody_touches_is_left_alone(settings):
    """The follower must be inert when the operator changes nothing.

    This is what makes it safe to put in the sweep of a coordinator driving real
    work: it cannot alter a run that does not involve someone changing their mind.
    """
    _write(settings, disk_floor_gb=4)
    calls = []
    f = Follower(lambda: settings).watch("disk_floor_gb", calls.append)
    f.prime()
    for _ in range(50):
        assert f.poll() == []
    assert calls == []


def test_a_live_override_survives_until_the_operator_moves_that_knob(settings):
    """強制開始 zeroes the disk gate live. The file still says 1. The 0 must hold."""
    _write(settings, disk_floor_gb=1)
    box = [1.0]
    f = Follower(lambda: settings).watch("disk_floor_gb", lambda v: box.__setitem__(0, v))
    f.prime()

    box[0] = 0.0                               # the cockpit's 強制開始, pushed live
    for _ in range(10):
        f.poll()
    assert box[0] == 0.0, "following the file undid the operator's live override"

    _write(settings, disk_floor_gb=3)          # now they move the knob: they win
    assert f.poll() == [("disk_floor_gb", 3.0)]
    assert box[0] == 3.0


def test_a_key_the_file_does_not_mention_is_not_an_opinion(settings):
    """Silence is not the same as 'set it to the default'.

    Reading an absent key as a value would overwrite a live value with a number
    nobody chose -- which is how a default quietly becomes a setting.
    """
    _write(settings, ram_floor_mb=512)
    box = [7.0]
    f = Follower(lambda: settings).watch("disk_floor_gb", lambda v: box.__setitem__(0, v))
    f.prime()
    _write(settings, ram_floor_mb=256)
    assert f.poll() == []
    assert box[0] == 7.0


def test_a_half_typed_value_is_not_adopted_and_is_not_forgotten(settings):
    """The cockpit rewrites the whole file, so a read can land mid-save.

    A value that will not parse must be skipped rather than raise -- and must not be
    recorded as seen, or the finished value that follows would look like no change
    at all and never be adopted.
    """
    _write(settings, disk_floor_gb=1)
    box = [1.0]
    f = Follower(lambda: settings).watch("disk_floor_gb", lambda v: box.__setitem__(0, v))
    f.prime()

    _write(settings, disk_floor_gb="")         # mid-save
    assert f.poll() == []
    assert box[0] == 1.0

    _write(settings, disk_floor_gb=2)
    assert f.poll() == [("disk_floor_gb", 2.0)]
    assert box[0] == 2.0


def test_a_missing_settings_file_changes_nothing(settings):
    """No APPDATA, no file, no opinion -- and no crash inside a live sweep."""
    box = [6.0]
    f = Follower(lambda: settings).watch("disk_floor_gb", lambda v: box.__setitem__(0, v))
    f.prime()
    assert f.poll() == []
    assert box[0] == 6.0


def test_an_applier_that_raises_does_not_end_the_run(settings):
    """A coordinator sweep is driving real work; one bad knob must not stop it."""
    _write(settings, disk_floor_gb=1)

    def boom(_v):
        raise RuntimeError("no")

    reached = []
    f = (Follower(lambda: settings)
         .watch("disk_floor_gb", boom)
         .watch("ram_floor_mb", reached.append))
    f.prime()
    _write(settings, disk_floor_gb=2, ram_floor_mb=512)
    adopted = f.poll()
    assert reached == [512.0], "a raising applier cost the key behind it"
    assert ("disk_floor_gb", 2.0) not in adopted


def test_the_coordinator_follows_the_floors_and_the_tab_cap():
    """The three knobs that were latched are the three that are followed.

    Source-level, because the wiring lives inside a 700-line run() that cannot be
    called in a test. A fourth latched knob added later will not be caught by this
    -- what it does catch is the wiring being dropped, which is how the first fix
    for a settings bug usually dies.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "fleet_runner.py"), encoding="utf-8").read()
    assert "settings_follower.poll()" in src, "the follower is built but never polled"
    i_poll = src.index("settings_follower.poll()")
    i_cmds = src.index("_drain_commands(workers)", src.index("def on_tick"))
    assert i_poll < i_cmds, (
        "settings are followed AFTER the cockpit's commands are drained, so a push and "
        "a file save made by the same operator action race to a different answer")
    for key in ("disk_floor_gb", "ram_floor_mb", "maxtabs"):
        assert '.watch("%s"' % key in src, "%s went back to being read once at launch" % key

def test_the_helper_moves_the_stamp_even_for_two_writes_in_one_tick(settings):
    """PINS THE FIX, rather than trusting that ten green runs mean anything.

    The failure it replaces appeared about one run in several, so counting passes could not
    distinguish "fixed" from "lucky". What can be asserted is the property the tests actually
    depend on: two writes of the same-sized content must never present the follower with the
    same cache key, no matter how close together they happen.
    """
    _write(settings, disk_floor_gb=1)
    first = os.stat(settings)
    _write(settings, disk_floor_gb=3)          # same length, deliberately
    second = os.stat(settings)
    assert first.st_size == second.st_size, "this test is only interesting at equal size"
    assert second.st_mtime > first.st_mtime, (
        "two writes produced the same stamp; a real change would be invisible to the follower")


def test_the_coordinator_wiring_moves_the_boxes_a_run_actually_reads(settings):
    """THE WIRING, exercised rather than read.

    relay/test_a_setting_the_operator_changed_reaches_a_running_fleet's older coordinator
    check is source-level and says why: the wiring lived inside a 700-line run(). Reading
    source cannot catch a callback that writes into a box nothing reads, which gpt-6-astra
    named as the gap in these tests. fleet_runner.build_settings_follower now has a name, so
    the file-to-box half can be exercised with the SAME callbacks production uses.

    What is still not proven here is box-to-decision: relay_fleet indexes these list objects
    at the moment it admits (relay_fleet.py:7978, 7980, 8029, 8067), and that is asserted from
    source. Saying so is the point -- an untested half named is a different thing from an
    untested half assumed.
    """
    from relay.fleet_runner import build_settings_follower

    disk_box, ram_box, mc_box, asc_box = [6.0], [512.0], [3], [False, 100]
    _write(settings, disk_floor_gb=6, ram_floor_mb=512, maxtabs=3)
    f = build_settings_follower(disk_box, ram_box, mc_box, asc_box, path_fn=lambda: settings)

    _write(settings, disk_floor_gb=2, ram_floor_mb=1024, maxtabs=5)
    f.poll()
    assert disk_box[0] == 2.0
    assert ram_box[0] == 1024.0
    assert mc_box[0] == 5, "with autoscale off, maxtabs is the fixed cap"
    assert asc_box[1] == 100, "and it must not have moved the autoscale ceiling"


def test_under_autoscale_the_tab_knob_moves_the_ceiling_instead(settings):
    """The same key means two things, which is why its declaration says so. Under autoscale
    the fixed cap is computed from RAM each loop, so writing it there would be overwritten a
    second later and the operator's change would appear to do nothing."""
    from relay.fleet_runner import build_settings_follower

    disk_box, ram_box, mc_box, asc_box = [6.0], [512.0], [3], [True, 100]
    _write(settings, maxtabs=3)
    f = build_settings_follower(disk_box, ram_box, mc_box, asc_box, path_fn=lambda: settings)
    _write(settings, maxtabs=8)
    f.poll()
    assert asc_box[1] == 8, "under autoscale the tab knob is the ceiling"
    assert mc_box[0] == 3, "and the live cap is left to the RAM computation"
