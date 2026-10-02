# -*- coding: utf-8 -*-
"""`fanout_write_scope` agrees in every layer, is reachable from the GUI, and is shadow only.

Owner rule: a setting with no GUI path does not exist. The registry (tools/settings_keys.py),
the reader (relay/write_scope.py) and the cockpit (ui/EffortPolicy.cs + ui/FleetCockpit.cs) are
compared here, the way tests/test_fanout_default_on_agrees.py compares the fan-out default.
There is deliberately no enforcing value anywhere: enforcement would run through the folder
policy and needs its own approval.
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
from relay import write_scope as WS  # noqa: E402
from tools import settings_keys as SK  # noqa: E402


def _read(*parts):
    with io.open(os.path.join(REPO, *parts), encoding="utf-8-sig") as fh:
        return fh.read()


def test_the_registry_declares_it_each_gate_default_off():
    assert SK.effect("fanout_write_scope") == SK.EACH_GATE
    assert SK.default("fanout_write_scope") == "off"
    assert WS.KEY == "fanout_write_scope"


def test_the_cockpit_names_the_same_key_modes_and_default():
    src = _read("ui", "EffortPolicy.cs")
    m = re.search(r'class WriteScopeView.*?public const string Key = "([a-z_]+)";\s*public const string '
                  r'Default = "([a-z]+)";\s*public static readonly string\[\] Modes = \{([^}]*)\};', src, re.S)
    assert m, "WriteScopeView not found"
    assert m.group(1) == WS.KEY
    assert m.group(2) == SK.default("fanout_write_scope")
    assert tuple(re.findall(r'"([a-z]+)"', m.group(3))) == WS.MODES


def test_no_layer_offers_an_enforcing_value():
    src = _read("ui", "EffortPolicy.cs")
    view = src[src.index("class WriteScopeView"):src.index("class GroupTreeView")]
    assert 'IsMode(string v) { return v == "off" || v == "shadow"; }' in view
    assert "enforce" not in view.lower() and '"on"' not in view
    note = SK.KEYS["fanout_write_scope"].note
    assert "nothing is blocked" in note


def test_the_cockpit_loads_persists_paints_and_does_not_refire():
    src = _read("ui", "FleetCockpit.cs")
    # lives in the gear popup (BuildSettingsPanel), NOT in the header
    assert "WriteScopeControl()" in src and "col.Children.Add(WriteScopeControl());" in src
    assert "ctrls.Children.Add(WriteScopeControl());" not in src
    assert "WriteScopeView.ParseLine(ln)" in src                    # settings load
    assert "SaveKey(WriteScopeView.Key, _wsVal)" in src             # persist, SaveKey only
    m = re.search(r"void PaintWriteScope\(\).*?\n    }\n", src, re.S)
    assert m and "if (!Equals(ComboVal(_wsBox), _wsVal)) ComboSelectVal(_wsBox, _wsVal);" in m.group(0)
    assert 'Obj(root, "fanout_write_scope")' in src                  # what the runner reports
    assert "PaintWriteScopeInEffect(root);" in src                   # on every status render
    assert re.search(r'case "fanout_write_scope":', src)             # timing table
    # the control writes the setting only through SaveKey: no direct file writes of its own
    block = src[src.index("UIElement WriteScopeControl()"):src.index("void PaintWriteScopeInEffect")]
    assert "File." not in block and "WriteAllText" not in block


def test_the_tooltip_says_nothing_is_blocked_in_both_languages():
    src = _read("ui", "EffortPolicy.cs")
    assert "Record only: nothing is blocked." in src
    assert "記録のみ: 何もブロックしません" in src


def test_the_status_snapshot_carries_the_report(tmp_path, monkeypatch):
    p = tmp_path / "settings.txt"
    p.write_text("fanout_write_scope=shadow\n", encoding="utf-8")
    WS._reset_for_test()
    monkeypatch.setattr("tools.settings_path.settings_file", lambda: str(p))
    assert FR._write_scope_block() == {"fanout_write_scope": {"mode": "shadow", "overlaps_seen": 0}}
    p.write_text("fanout_write_scope=off\n", encoding="utf-8")
    assert FR._write_scope_block()["fanout_write_scope"]["mode"] == "off"


def test_the_sweep_calls_the_recorder_and_the_snapshot_calls_the_report():
    src = _read("relay", "fleet_runner.py")
    assert "_write_scope_tick(workers)" in src
    assert "_snap.update(_write_scope_block())" in src


def test_the_cockpit_still_compiles_with_the_control():
    from bench import ui_build_check as B
    if not os.path.isfile(B.CSC):
        if os.environ.get("REQUIRE_CSC") == "1":
            pytest.fail("csc missing")
        pytest.skip("csc not available")
    import tempfile
    out = tempfile.mkdtemp(prefix="wscope_build_")
    try:
        for name, srcs in B.targets_from_rebuild_script():
            if name != "FleetCockpit":
                continue
            r = B.build(name, srcs, out)
            assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    finally:
        shutil.rmtree(out, ignore_errors=True)
