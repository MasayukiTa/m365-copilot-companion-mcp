# -*- coding: utf-8 -*-
"""`fanout_load_routing` agrees in every layer, is reachable from the GUI, and is shadow only.

Owner rule: a setting with no GUI path does not exist. The registry (tools/settings_keys.py),
the reader (relay/load_routing.py) and the cockpit (ui/EffortPolicy.cs + the cockpit partial
classes) are compared here, the way tests/test_fanout_write_scope_agrees.py compares the sibling
write scope. There is deliberately no value that routes anywhere: the router records what it
would have chosen and nothing else.
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
from relay import load_routing as LR  # noqa: E402
from tools import cockpit_source  # noqa: E402  (the cockpit source is split across files)
from tools import settings_keys as SK  # noqa: E402


def _read(*parts):
    with io.open(os.path.join(REPO, *parts), encoding="utf-8-sig") as fh:
        return fh.read()


def test_the_registry_declares_it_each_gate_default_off():
    assert SK.effect("fanout_load_routing") == SK.EACH_GATE
    assert SK.default("fanout_load_routing") == "off"
    assert LR.KEY == "fanout_load_routing"
    assert LR.MODES == ("off", "shadow")


def test_the_cockpit_names_the_same_key_modes_and_default():
    src = _read("ui", "EffortPolicy.cs")
    m = re.search(r'class LoadRoutingView.*?public const string Key = "([a-z_]+)";\s*public const '
                  r'string Default = "([a-z]+)";\s*public static readonly string\[\] Modes = '
                  r'\{([^}]*)\};', src, re.S)
    assert m, "LoadRoutingView not found"
    assert m.group(1) == LR.KEY
    assert m.group(2) == SK.default("fanout_load_routing")
    assert tuple(re.findall(r'"([a-z]+)"', m.group(3))) == LR.MODES


def test_no_layer_offers_a_value_that_routes():
    src = _read("ui", "EffortPolicy.cs")
    view = src[src.index("class LoadRoutingView"):]
    view = view[:view.index("\n}\n")]
    assert 'IsMode(string v) { return v == "off" || v == "shadow"; }' in view
    assert "enforce" not in view.lower() and '"on"' not in view and '"route"' not in view
    note = SK.KEYS["fanout_load_routing"].note
    assert "nothing is reordered" in note and "no prompt or output changes" in note


def test_the_cockpit_loads_persists_paints_and_does_not_refire():
    src = cockpit_source.read(encoding="utf-8-sig")
    # lives in the gear popup (BuildSettingsPanel), NOT in the header
    assert "col.Children.Add(LoadRoutingControl());" in src
    assert "ctrls.Children.Add(LoadRoutingControl());" not in src
    assert "LoadRoutingView.ParseLine(ln)" in src                    # settings load
    assert "SaveKey(LoadRoutingView.Key, _lrVal)" in src             # persist, SaveKey only
    m = re.search(r"void PaintLoadRouting\(\).*?\n    }\n", src, re.S)
    assert m and "if (!Equals(ComboVal(_lrBox), _lrVal)) ComboSelectVal(_lrBox, _lrVal);" in m.group(0)
    assert 'Obj(root, "fanout_load_routing")' in src                 # what the runner reports
    assert "PaintLoadRoutingInEffect(root);" in src                  # on every status render
    assert re.search(r'case "fanout_load_routing":', src)            # timing table
    assert 'SetAutomationId(_lrBox, "loadRoutingBox")' in src        # reachable by the UI driver
    # the control writes the setting only through SaveKey: no direct file writes of its own
    block = src[src.index("UIElement LoadRoutingControl()"):src.index("void PaintLoadRoutingInEffect")]
    assert "File." not in block and "WriteAllText" not in block


def test_the_ui_driver_script_knows_the_box():
    src = _read("scripts", "win", "set_setting_via_ui.ps1")
    assert re.search(r"'fanout_load_routing'\s*=\s*'loadRoutingBox'", src)


def test_the_tooltip_says_assignment_is_unchanged_in_both_languages():
    src = _read("ui", "EffortPolicy.cs")
    assert "Record only: assignment is unchanged." in src
    assert "記録のみ: 割当は変わりません" in src


def test_the_status_snapshot_carries_the_report(tmp_path, monkeypatch):
    p = tmp_path / "settings.txt"
    p.write_text("fanout_load_routing=shadow\n", encoding="utf-8")
    LR._reset_for_test()
    monkeypatch.setattr("tools.settings_path.settings_file", lambda: str(p))
    assert FR._load_routing_block() == {"fanout_load_routing":
                                        {"mode": "shadow", "recorded": 0, "differed": 0}}
    p.write_text("fanout_load_routing=off\n", encoding="utf-8")
    assert FR._load_routing_block()["fanout_load_routing"]["mode"] == "off"
    LR._reset_for_test()


def test_the_snapshot_calls_the_report():
    src = _read("relay", "fleet_runner.py")
    assert "_snap.update(_load_routing_block())" in src


def test_the_cockpit_still_compiles_with_the_control():
    from bench import ui_build_check as B
    if not os.path.isfile(B.CSC):
        if os.environ.get("REQUIRE_CSC") == "1":
            pytest.fail("csc missing")
        pytest.skip("csc not available")
    import tempfile
    out = tempfile.mkdtemp(prefix="lroute_build_")
    try:
        for name, srcs in B.targets_from_rebuild_script():
            if name != "FleetCockpit":
                continue
            r = B.build(name, srcs, out)
            assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    finally:
        shutil.rmtree(out, ignore_errors=True)
