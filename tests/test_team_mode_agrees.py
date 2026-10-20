# -*- coding: utf-8 -*-
"""`team_mode` agrees in every layer, is reachable from the GUI, and is shadow only.

Owner rule: a setting with no GUI path does not exist. The registry (tools/settings_keys.py),
the reader (relay/team_formation.py) and the cockpit (ui/EffortPolicy.cs + the cockpit partial
classes) are compared here, the way tests/test_fanout_load_routing_agrees.py compares load-aware
routing. There is deliberately no value that forms a team: the split records what a team-forming
rule would have proposed and nothing else.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import fleet_runner as FR  # noqa: E402
from relay import team_formation as TF  # noqa: E402
from tools import cockpit_source  # noqa: E402  (the cockpit source is split across files)
from tools import settings_keys as SK  # noqa: E402


def _read(*parts):
    with io.open(os.path.join(REPO, *parts), encoding="utf-8-sig") as fh:
        return fh.read()


def test_the_registry_declares_it_each_gate_default_off():
    assert SK.effect("team_mode") == SK.EACH_GATE
    assert SK.default("team_mode") == "off"
    assert TF.KEY == "team_mode"
    assert TF.MODES == ("off", "shadow")


def test_the_cockpit_names_the_same_key_modes_and_default():
    src = _read("ui", "EffortPolicy.cs")
    m = re.search(r'class TeamModeView.*?public const string Key = "([a-z_]+)";\s*public const '
                  r'string Default = "([a-z]+)";\s*public static readonly string\[\] Modes = '
                  r'\{([^}]*)\};', src, re.S)
    assert m, "TeamModeView not found"
    assert m.group(1) == TF.KEY
    assert m.group(2) == SK.default("team_mode")
    assert tuple(re.findall(r'"([a-z]+)"', m.group(3))) == TF.MODES


def test_no_layer_offers_a_value_that_forms_a_team():
    src = _read("ui", "EffortPolicy.cs")
    view = src[src.index("class TeamModeView"):]
    view = view[:view.index("\n}\n")]
    assert 'IsMode(string v) { return v == "off" || v == "shadow"; }' in view
    assert "enforce" not in view.lower() and '"on"' not in view and '"form"' not in view
    note = SK.KEYS["team_mode"].note
    assert "unchanged" in note and "no goal text" in note


def test_the_cockpit_loads_persists_paints_and_does_not_refire():
    src = cockpit_source.read(encoding="utf-8-sig")
    # lives in the gear popup (BuildSettingsPanel), NOT in the header
    assert "col.Children.Add(TeamModeControl());" in src
    assert "ctrls.Children.Add(TeamModeControl());" not in src
    assert "TeamModeView.ParseLine(ln)" in src                       # settings load
    assert "SaveKey(TeamModeView.Key, _tmVal)" in src                # persist, SaveKey only
    m = re.search(r"void PaintTeamMode\(\).*?\n    }\n", src, re.S)
    assert m and "if (!Equals(ComboVal(_tmBox), _tmVal)) ComboSelectVal(_tmBox, _tmVal);" in m.group(0)
    assert 'Obj(root, "team_mode")' in src                           # what the runner reports
    assert "PaintTeamModeInEffect(root);" in src                     # on every status render
    assert re.search(r'case "team_mode":', src)                      # timing table
    assert 'SetAutomationId(_tmBox, "teamModeBox")' in src           # reachable by the UI driver
    block = src[src.index("UIElement TeamModeControl()"):src.index("void PaintTeamModeInEffect")]
    assert "File." not in block and "WriteAllText" not in block


def test_the_ui_driver_script_knows_the_box():
    src = _read("scripts", "win", "set_setting_via_ui.ps1")
    assert re.search(r"'team_mode'\s*=\s*'teamModeBox'", src)


def test_the_tooltip_says_the_split_is_unchanged_in_both_languages():
    src = _read("ui", "EffortPolicy.cs")
    assert "Record only: the split is unchanged." in src
    assert "記録のみ: 分割の中身は変わりません" in src


def test_the_status_snapshot_carries_the_report(tmp_path, monkeypatch):
    p = tmp_path / "settings.txt"
    p.write_text("team_mode=shadow\n", encoding="utf-8")
    TF._reset_for_test()
    monkeypatch.setattr("tools.settings_path.settings_file", lambda: str(p))
    assert FR._team_formation_block() == {"team_mode": {"mode": "shadow", "recorded": 0, "differed": 0}}
    p.write_text("team_mode=off\n", encoding="utf-8")
    assert FR._team_formation_block()["team_mode"]["mode"] == "off"
    TF._reset_for_test()


def test_the_snapshot_calls_the_report():
    assert "_snap.update(_team_formation_block())" in _read("relay", "fleet_runner.py")


def test_the_cockpit_still_compiles_with_the_control():
    from bench import ui_build_check as B
    if not os.path.isfile(B.CSC):
        if os.environ.get("REQUIRE_CSC") == "1":
            pytest.fail("csc missing")
        pytest.skip("csc not available")
    import tempfile
    out = tempfile.mkdtemp(prefix="team_mode_build_")
    try:
        for name, srcs in B.targets_from_rebuild_script():
            if name != "FleetCockpit":
                continue
            r = B.build(name, srcs, out)
            assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    finally:
        shutil.rmtree(out, ignore_errors=True)
