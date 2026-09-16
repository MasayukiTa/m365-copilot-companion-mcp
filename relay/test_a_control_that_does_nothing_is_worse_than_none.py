# -*- coding: utf-8 -*-
"""Two settings the operator can see and could not affect.

Found by auditing every key in the cockpit's settings.txt against its readers, after one of
them (disk_floor_gb) turned out not to reach the fleet at all.

FAN-OUT. The cockpit has a fan-out switch, writes `fanout=on|off` from it, and honours it for
launches from its own button. The AUTOSTART path -- which is where goals from the tunnel
arrive, i.e. most of them -- never read the key: it decided on goal length alone. So
settings.txt said `fanout=off` while every autostarted coordinator on 2026-09-16 carried
`--fanout`. `_wants_fanout`'s docstring promised "yes, unless an operator said otherwise" and
listed an environment variable and a kill switch as the ways to say otherwise; the control in
front of the operator was not one of them.

A DISABLED GATE REPORTED SOMEBODY ELSE'S FLOOR. `float(floor_gb or DEFAULT_DISK_FLOOR_GB)`
treats 0.0 as absent, and 0.0 is exactly what the cockpit's 強制開始 writes to turn the disk
gate off. The function that DECIDES admission distinguishes them with `is None`; the sentence
that EXPLAINS a deferral did not, so the one output a human reads claimed a 6 GB floor for a
run that had no floor at all.
"""
from __future__ import annotations

import io
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import fleet_runner as FR  # noqa: E402
from relay import task_router as TR  # noqa: E402


def _settings(tmp_path, **keys):
    """A settings.txt in the cockpit's own format, pointed at by _settings_path."""
    path = tmp_path / "settings.txt"
    body = "".join("%s=%s\n" % (k, v) for k, v in keys.items())
    io.open(str(path), "w", encoding="utf-8", newline="\n").write(body)
    return str(path)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    def _use(**keys):
        p = _settings(tmp_path, **keys)
        monkeypatch.setattr(FR, "_settings_path", lambda: p)
        return p
    return _use


# --------------------------------------------------------------------------- the switch
def test_the_operators_fanout_switch_is_read_at_all(settings):
    settings(fanout="off")
    assert FR.settings_fanout() is False
    settings(fanout="on")
    assert FR.settings_fanout() is True


def test_an_untouched_switch_is_not_a_choice(settings):
    """None, not False. "Nobody has chosen" must stay distinguishable from "chosen off", or
    the caller cannot tell which of its own fallbacks applies."""
    settings(dark="0")
    assert FR.settings_fanout() is None


@pytest.mark.parametrize("goal_len", [1, 900])
def test_off_beats_the_length_rule_in_both_directions(settings, monkeypatch, goal_len):
    """THE MEASURED CASE. settings.txt said off; every autostarted run carried --fanout,
    because the length rule was the only thing consulted."""
    settings(fanout="off")
    monkeypatch.delenv("FLEET_INTAKE_AUTOSTART_FANOUT", raising=False)
    assert TR._wants_fanout([{"goal": "x" * goal_len}]) is False


@pytest.mark.parametrize("goal_len", [1, 900])
def test_on_beats_the_length_rule_too(settings, monkeypatch, goal_len):
    settings(fanout="on")
    monkeypatch.delenv("FLEET_INTAKE_AUTOSTART_FANOUT", raising=False)
    assert TR._wants_fanout([{"goal": "x" * goal_len}]) is True


def test_an_unset_switch_leaves_the_old_behaviour_exactly_as_it_was(settings, monkeypatch):
    """A machine where nobody touched the control must behave as before -- the whole point of
    returning None rather than a default."""
    settings(dark="0")
    monkeypatch.delenv("FLEET_INTAKE_AUTOSTART_FANOUT", raising=False)
    assert TR._wants_fanout([{"goal": "x" * 900}]) is True


def test_the_environment_override_still_wins(settings, monkeypatch):
    """It is documented as winning outright, and a settings key must not quietly outrank it."""
    settings(fanout="on")
    monkeypatch.setenv("FLEET_INTAKE_AUTOSTART_FANOUT", "0")
    assert TR._wants_fanout([{"goal": "x" * 900}]) is False


def test_an_unreadable_settings_file_does_not_break_a_launch(monkeypatch):
    monkeypatch.setattr(FR, "_settings_path", lambda: "/nonexistent/settings.txt")
    monkeypatch.delenv("FLEET_INTAKE_AUTOSTART_FANOUT", raising=False)
    assert FR.settings_fanout() is None
    assert TR._wants_fanout([{"goal": "x" * 900}]) is True


# ------------------------------------------------------------------- the disabled gate
def test_a_disabled_disk_gate_is_reported_as_disabled(capsys, monkeypatch):
    """0.0 is falsy, and 0.0 is what 強制開始 writes. The alert said 6 GB."""
    from relay import relay_fleet as RF

    monkeypatch.setattr(RF, "free_disk_gb", lambda *a, **k: 1.23)
    monkeypatch.setattr(RF, "_DISK_DEFER_SINCE", [0.0])
    monkeypatch.setattr(RF, "_DISK_DEFER_LAST", [0.0])
    monkeypatch.setattr(RF, "_DISK_DEFER_NOTIFIED", [0.0])

    RF._note_disk_defer(0.0, 1, notify=lambda *a, **k: None, now=1_000_000.0)
    said = capsys.readouterr().out
    assert "floor 0.0 GB" in said, said
    assert "floor 6.0 GB" not in said


def test_a_missing_floor_still_falls_back_to_the_default(capsys, monkeypatch):
    """None means "not supplied", and that is the case the fallback was written for."""
    from relay import relay_fleet as RF

    monkeypatch.setattr(RF, "free_disk_gb", lambda *a, **k: 1.23)
    monkeypatch.setattr(RF, "_DISK_DEFER_SINCE", [0.0])
    monkeypatch.setattr(RF, "_DISK_DEFER_LAST", [0.0])
    monkeypatch.setattr(RF, "_DISK_DEFER_NOTIFIED", [0.0])

    RF._note_disk_defer(None, 1, notify=lambda *a, **k: None, now=1_000_000.0)
    said = capsys.readouterr().out
    assert ("floor %.1f GB" % RF.DEFAULT_DISK_FLOOR_GB) in said, said
