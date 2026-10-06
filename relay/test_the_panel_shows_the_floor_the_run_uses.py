# -*- coding: utf-8 -*-
"""The disk floor a run uses must be the one the settings panel shows.

MEASURED 2026-09-15. The cockpit's 「実行下限ディスク (GB)」 read 1, the operator had set it
there, and an autostarted run used 2. fleet_runner resolves the floor as
"CLI --disk-floor-gb >= 0 -> settings.txt disk_floor_gb -> env", so passing the flag at all
beats the control the panel is showing. It was worse before -- the same override at 0, further
from the setting -- so on this path the operator's choice had never once been honoured.

A panel that displays a number the machine does not use is the defect class this session spent
its length removing: a permanently-amber button, a catalogue coverage metric measured against a
denominator that had drifted, an approval queue of two thousand cards nobody decided. Adding one
more was not the way to end the day.

THE CLAMP IS WHY 0 COUNTS AS A CHOICE. `SetDiskFloor` clamps to 0..100, so the cockpit offers 0
and documents what it means ("a 0 floor disables the disk gate"). Reading 0 as "unset" and
substituting something else would be the same substitution in a smaller coat.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import pytest  # noqa: E402

from relay import task_router as TR  # noqa: E402


@pytest.fixture
def state(tmp_path, monkeypatch):
    """The same setup relay/test_fleet_autostart.py uses -- autostart refuses without an agent
    URL and its own directories, and a refusal never reaches the launcher at all."""
    sd = tmp_path / "fleet"
    sd.mkdir()
    monkeypatch.setattr(TR, "TASKS", str(tmp_path / "tasks"))
    monkeypatch.setattr(TR, "FLEET_STATE_DIR", str(sd))
    monkeypatch.setattr(TR, "AUTOSTART", True)
    monkeypatch.setenv("MCP_FLEET_AGENT_URL", "https://example.invalid/agent")
    TR.ensure_dirs()
    return sd


class _Launcher:
    """Stands in for Popen: a test that really started a fleet would open a browser."""

    def __init__(self):
        self.cmd = None

    def __call__(self, cmd):
        self.cmd = cmd
        return 4321


def _launch(monkeypatch, state, floor):
    """Run autostart with `floor` as what settings.txt would answer (-1 = no line at all)."""
    monkeypatch.setattr(TR, "_operator_set_a_disk_floor", lambda: floor >= 0)
    launcher = _Launcher()
    plan = TR.autostart_fleet([{"text": "goal", "priority": False}], str(state),
                              launcher=launcher)
    assert launcher.cmd is not None, "autostart never reached the launcher: %s" % (plan,)
    return launcher.cmd


def test_an_operators_floor_is_left_to_the_settings_file(monkeypatch, state):
    cmd = _launch(monkeypatch, state, 1.0)
    assert "--disk-floor-gb" not in cmd, (
        "the flag beats settings.txt, so passing it overrides the number the panel shows: %s"
        % cmd)


def test_a_floor_of_zero_is_a_choice_and_is_also_left_alone(monkeypatch, state):
    """The cockpit clamps to 0..100 and says what 0 means, so 0 is offered, not absent."""
    cmd = _launch(monkeypatch, state, 0.0)
    assert "--disk-floor-gb" not in cmd


def test_with_nothing_chosen_the_autostart_default_is_passed(monkeypatch, state):
    """The case the hard-coding was written for: a tunnel goal would otherwise inherit the
    bench reserve and admit nothing, silently."""
    cmd = _launch(monkeypatch, state, -1.0)
    assert "--disk-floor-gb" in cmd
    assert float(cmd[cmd.index("--disk-floor-gb") + 1]) == TR.AUTOSTART_DISK_FLOOR_GB


def test_the_autostart_default_is_between_disabled_and_the_bench_reserve():
    """Zero let C: reach zero bytes; the bench reserve blocked everything for twenty-five
    silent minutes. The default has to be neither."""
    from relay.relay_fleet import DEFAULT_DISK_FLOOR_GB
    assert 0 < TR.AUTOSTART_DISK_FLOOR_GB < DEFAULT_DISK_FLOOR_GB


def test_an_unreadable_settings_file_does_not_read_as_a_choice(monkeypatch):
    """"I could not tell" must not be mistaken for "they chose nothing to reserve"."""
    import relay.fleet_runner as FR

    def _boom(*a, **k):
        raise OSError("settings unreadable")

    monkeypatch.setattr(FR, "settings_disk_floor", _boom)
    assert TR._operator_set_a_disk_floor() is False


def test_a_real_settings_value_is_detected_as_a_choice(monkeypatch):
    import relay.fleet_runner as FR
    monkeypatch.setattr(FR, "settings_disk_floor", lambda default=None: 1.0)
    assert TR._operator_set_a_disk_floor() is True
    # and the sentinel path: no line in the file means the default comes back unchanged
    monkeypatch.setattr(FR, "settings_disk_floor", lambda default=None: default)
    assert TR._operator_set_a_disk_floor() is False
