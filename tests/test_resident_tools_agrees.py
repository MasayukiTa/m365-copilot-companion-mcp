# -*- coding: utf-8 -*-
"""`resident_tools` agrees in every layer, is reachable from the GUI, and nothing acts on it.

The first slice of the resident component adds only the refusal rules and an observation probe.
The setting is declared and shown, and status.json reports it; no product code reads it to decide
anything. Owner rule: a setting with no GUI path does not exist.
"""
from __future__ import annotations

import io
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import fleet_runner as FR  # noqa: E402
from relay import resident_guard as RG  # noqa: E402
from tools import cockpit_source  # noqa: E402
from tools import settings_keys as SK  # noqa: E402


def _read(*parts):
    with io.open(os.path.join(REPO, *parts), encoding="utf-8-sig") as fh:
        return fh.read()


def test_the_registry_declares_it_each_gate_default_off():
    assert SK.effect("resident_tools") == SK.EACH_GATE
    assert SK.default("resident_tools") == "off"
    assert RG.KEY == "resident_tools" and RG.DEFAULT == "off" and RG.MODES == ("off", "on")


def test_the_cockpit_names_the_same_key_modes_and_default():
    src = _read("ui", "EffortPolicy.cs")
    m = re.search(r'class ResidentToolsView.*?public const string Key = "([a-z_]+)";\s*public const string '
                  r'Default = "([a-z]+)";\s*public static readonly string\[\] Modes = \{([^}]*)\};', src, re.S)
    assert m, "ResidentToolsView not found"
    assert m.group(1) == RG.KEY
    assert m.group(2) == SK.default("resident_tools")
    assert tuple(re.findall(r'"([a-z]+)"', m.group(3))) == RG.MODES


def test_the_cockpit_loads_persists_and_paints_without_refiring():
    src = cockpit_source.read(encoding="utf-8-sig")
    assert "col.Children.Add(ResidentToolsControl());" in src
    assert "ctrls.Children.Add(ResidentToolsControl());" not in src          # popup, not header
    assert "ResidentToolsView.ParseLine(ln)" in src                           # settings load
    assert "SaveKey(ResidentToolsView.Key, _rtVal)" in src                    # persist, SaveKey only
    assert 'SetAutomationId(_rtBox, "residentToolsBox")' in src
    assert re.search(r'case "resident_tools":', src)                          # timing table
    m = re.search(r"void PaintResidentTools\(\).*?\n    }\n", src, re.S)
    assert m and "if (!Equals(ComboVal(_rtBox), _rtVal)) ComboSelectVal(_rtBox, _rtVal);" in m.group(0)
    block = src[src.index("UIElement ResidentToolsControl()"):src.index("void PaintResidentTools()")]
    assert "File." not in block and "WriteAllText" not in block


def test_the_status_snapshot_reports_the_setting(tmp_path, monkeypatch):
    p = tmp_path / "settings.txt"
    p.write_text("resident_tools=on\n", encoding="utf-8")
    RG._reset_for_test()
    monkeypatch.setattr("tools.settings_path.settings_file", lambda: str(p))
    assert FR._resident_tools_block() == {"resident_tools": "on"}
    p.write_text("resident_tools=off\n", encoding="utf-8")
    assert FR._resident_tools_block() == {"resident_tools": "off"}
    assert "_snap.update(_resident_tools_block())" in _read("relay", "fleet_runner.py")


def test_nothing_in_the_product_decides_anything_from_the_value():
    """Only the reader itself, the status block and tests/probes mention the module's mode."""
    allowed = {os.path.normpath(p) for p in (
        "relay/resident_guard.py", "relay/fleet_runner.py", "relay/test_resident_guard.py",
        "tools/test_a_setting_declares_when_it_takes_effect.py", "tests/test_resident_tools_agrees.py",
        "scripts/probes/resident_observe.py", "scripts/probes/test_resident_observe.py",
        "tools/settings_keys.py", "scripts/test_set_setting_via_ui_ids.py")}
    hits = []
    for base in ("relay", "bridge", "tools", "scripts"):
        for root, _dirs, files in os.walk(os.path.join(REPO, base)):
            if ".venv" in root:
                continue
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                rel = os.path.normpath(os.path.relpath(path, REPO))
                if rel in allowed:
                    continue
                with io.open(path, encoding="utf-8", errors="replace") as fh:
                    if re.search(r"resident_guard|resident_tools", fh.read()):
                        hits.append(rel)
    assert hits == [], "unexpected readers of the resident setting: %s" % hits
