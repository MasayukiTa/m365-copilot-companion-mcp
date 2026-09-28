# -*- coding: utf-8 -*-
"""FleetCockpit's health subsystem stays physically separate from the giant window source.

This is a maintainability boundary, not a behavior test.  The 2026-09-28 audit measured the
single FleetCockpit.cs at ~15.5k lines / 425 methods; finding one health-state decision required
searching unrelated task cards, history, settings, runner launch, and evidence-spine code.  The
first extraction moved the contiguous health/signal/autofix block into a partial class without
changing the compiled CockpitWindow type.
"""
from pathlib import Path
import re

UI = Path(__file__).resolve().parent
MAIN = UI / "FleetCockpit.cs"
HEALTH = UI / "FleetCockpit.Health.cs"
BUILD = UI / "rebuild_ui.ps1"


def _text(path):
    return path.read_text(encoding="utf-8-sig")


def test_health_partial_is_a_real_build_input_of_the_same_window_class():
    main = _text(MAIN)
    health = _text(HEALTH)
    build = _text(BUILD)
    assert "partial class CockpitWindow : Window" in main
    assert "partial class CockpitWindow" in health
    m = re.search(r'Build\s+"FleetCockpit"\s+@\(([^\n]+)\)', build)
    assert m, "rebuild_ui.ps1 no longer exposes the FleetCockpit source list"
    sources = re.findall(r'"([^"]+\.cs)"', m.group(1))
    assert "FleetCockpit.cs" in sources
    assert "FleetCockpit.Health.cs" in sources


def test_health_and_autofix_methods_do_not_drift_back_into_the_main_file():
    main = _text(MAIN)
    health = _text(HEALTH)
    for signature in (
        "UIElement BuildHealthStrip()",
        "void PollHealthOnce()",
        "void PollToolProbeOnce(DateTime now)",
        "int AutoFixTargetDot()",
        "void RunFix()",
        "void RunStartAll()",
    ):
        assert signature in health, signature
        assert signature not in main, "health responsibility drifted back into FleetCockpit.cs: " + signature


def test_first_split_keeps_the_monolith_materially_smaller():
    main_lines = len(_text(MAIN).splitlines())
    health_lines = len(_text(HEALTH).splitlines())
    assert main_lines < 14000, "FleetCockpit.cs is regrowing past the first extraction boundary"
    assert 1800 <= health_lines <= 3500, "health partial changed size enough to require a boundary review"
    assert "Health strip, signal evaluation and auto-repair live in FleetCockpit.Health.cs." in _text(MAIN)
