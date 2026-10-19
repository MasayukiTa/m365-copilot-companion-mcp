# -*- coding: utf-8 -*-
"""`agent_board_mode` agrees in every layer and is reachable from the GUI.

Owner rule: a setting with no GUI path does not exist. The registry (tools/settings_keys.py), the
reader (relay/agent_board.py), the cockpit (ui/EffortPolicy.cs + the cockpit partial classes) and
the UI driver script are compared here, the way tests/test_fanout_load_routing_agrees.py compares
its sibling. p1 (workers write) is in the key's value set but is not offered by the selector and
is not implemented.
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

from relay import agent_board as AB  # noqa: E402
from relay import fleet_runner as FR  # noqa: E402
from tools import cockpit_source  # noqa: E402
from tools import settings_keys as SK  # noqa: E402


def _read(*parts):
    with io.open(os.path.join(REPO, *parts), encoding="utf-8-sig") as fh:
        return fh.read()


def test_the_registry_declares_it_sweep_start_default_off():
    assert SK.effect("agent_board_mode") == SK.SWEEP_START
    assert SK.default("agent_board_mode") == "off"
    assert AB.KEY == "agent_board_mode"
    assert AB.MODES == ("off", "p0", "p1")
    assert "not implemented" in SK.KEYS["agent_board_mode"].note


def test_the_default_is_the_one_the_reader_uses(tmp_path):
    assert AB.setting_mode(str(tmp_path / "missing.txt")) == SK.default("agent_board_mode")


def test_the_cockpit_names_the_same_key_default_and_the_offered_modes():
    src = _read("ui", "EffortPolicy.cs")
    m = re.search(r'class AgentBoardView.*?public const string Key = "([a-z_0-9]+)";\s*public const '
                  r'string Default = "([a-z0-9]+)";\s*public static readonly string\[\] Modes = '
                  r'\{([^}]*)\};', src, re.S)
    assert m, "AgentBoardView not found"
    assert m.group(1) == AB.KEY
    assert m.group(2) == SK.default("agent_board_mode")
    offered = tuple(re.findall(r'"([a-z0-9]+)"', m.group(3)))
    assert offered == AB.MODES[:2]              # p1 is declared in Python and not offered


def test_the_cockpit_loads_persists_paints_and_does_not_refire():
    src = cockpit_source.read(encoding="utf-8-sig")
    assert "col.Children.Add(AgentBoardControl());" in src
    assert "ctrls.Children.Add(AgentBoardControl());" not in src
    assert "AgentBoardView.ParseLine(ln)" in src
    assert "SaveKey(AgentBoardView.Key, _abVal)" in src
    m = re.search(r"void PaintAgentBoard\(\).*?\n    }\n", src, re.S)
    assert m and "if (!Equals(ComboVal(_abBox), _abVal)) ComboSelectVal(_abBox, _abVal);" in m.group(0)
    assert 'Obj(root, "agent_board")' in src
    assert "PaintAgentBoardInEffect(root);" in src
    assert re.search(r'case "agent_board_mode":', src)
    assert 'SetAutomationId(_abBox, "agentBoardBox")' in src
    block = src[src.index("UIElement AgentBoardControl()"):src.index("void PaintAgentBoardInEffect")]
    assert "File." not in block and "WriteAllText" not in block


def test_the_panel_calls_it_sweep_start_like_the_declaration():
    src = cockpit_source.read(encoding="utf-8-sig")
    i = src.index("static string SettingsTiming(string key)")
    block = src[i:src.index("string TimingBadge(string key)", i)]
    j = block.index('case "agent_board_mode":')
    assert re.search(r'return "([a-z_]+)";', block[j:]).group(1) == "sweep_start"


def test_the_ui_driver_script_knows_the_box():
    src = _read("scripts", "win", "set_setting_via_ui.ps1")
    assert re.search(r"'agent_board_mode'\s*=\s*'agentBoardBox'", src)


def test_the_tooltip_says_workers_cannot_write_in_both_languages():
    src = _read("ui", "EffortPolicy.cs")
    assert "Workers cannot write." in src and "ワーカーは書けません" in src


def test_the_snapshot_calls_the_report():
    assert "_snap.update(_agent_board_block())" in _read("relay", "fleet_runner.py")


def test_the_cockpit_still_compiles_with_the_control():
    from bench import ui_build_check as B
    if not os.path.isfile(B.CSC):
        if os.environ.get("REQUIRE_CSC") == "1":
            pytest.fail("csc missing")
        pytest.skip("csc not available")
    import tempfile
    out = tempfile.mkdtemp(prefix="board_build_")
    try:
        for name, srcs in B.targets_from_rebuild_script():
            if name != "FleetCockpit":
                continue
            r = B.build(name, srcs, out)
            assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    finally:
        shutil.rmtree(out, ignore_errors=True)
