# -*- coding: utf-8 -*-
"""`relay_findings` agrees in every layer, is reachable from the GUI, and is shadow only.

Owner rule: a setting with no GUI path does not exist. The registry (tools/settings_keys.py),
the reader (relay/relay_findings.py) and the cockpit (ui/EffortPolicy.cs + the cockpit partial
classes) are compared here, the way tests/test_team_mode_agrees.py compares team formation.
There is deliberately no value that builds a second stage: shadow records the findings a second
stage could have been handed and nothing else, and `on` is reserved and behaves as off.
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
from relay import relay_findings as RF  # noqa: E402
from tools import cockpit_source  # noqa: E402  (the cockpit source is split across files)
from tools import settings_keys as SK  # noqa: E402


def _read(*parts):
    with io.open(os.path.join(REPO, *parts), encoding="utf-8-sig") as fh:
        return fh.read()


def test_the_registry_declares_it_each_gate_default_off():
    assert SK.effect("relay_findings") == SK.EACH_GATE
    assert SK.default("relay_findings") == "off"
    assert RF.KEY == "relay_findings"
    assert RF.MODES == ("off", "shadow", "on")


def test_the_cockpit_names_the_same_key_modes_and_default():
    src = _read("ui", "EffortPolicy.cs")
    m = re.search(r'class RelayFindingsView.*?public const string Key = "([a-z_]+)";\s*public const '
                  r'string Default = "([a-z]+)";\s*public static readonly string\[\] Modes = '
                  r'\{([^}]*)\};', src, re.S)
    assert m, "RelayFindingsView not found"
    assert m.group(1) == RF.KEY
    assert m.group(2) == SK.default("relay_findings")
    assert tuple(re.findall(r'"([a-z]+)"', m.group(3))) == RF.MODES


def test_no_layer_offers_a_value_that_builds_a_second_stage():
    src = _read("ui", "EffortPolicy.cs")
    view = src[src.index("class RelayFindingsView"):]
    view = view[:view.index("\n}\n")]
    # on is OFFERED (it is a mode) but every layer labels it reserved and it behaves as off.
    assert 'IsMode(string v) { return v == "off" || v == "shadow" || v == "on"; }' in view
    assert "reserved" in view.lower()
    assert "build" not in view.lower() or "builds\n" not in view  # no builder wording that acts
    note = SK.KEYS["relay_findings"].note
    assert "reserved" in note and "behaves as off" in note
    assert "never the " in note and "goal text" in note  # the child's lines, never the goal text


def test_the_cockpit_loads_persists_paints_and_does_not_refire():
    src = cockpit_source.read(encoding="utf-8-sig")
    # lives in the gear popup (BuildSettingsPanel), NOT in the header
    assert "col.Children.Add(RelayFindingsControl());" in src
    assert "ctrls.Children.Add(RelayFindingsControl());" not in src
    assert "RelayFindingsView.ParseLine(ln)" in src                   # settings load
    assert "SaveKey(RelayFindingsView.Key, _rfVal)" in src            # persist, SaveKey only
    m = re.search(r"void PaintRelayFindings\(\).*?\n    }\n", src, re.S)
    assert m and "if (!Equals(ComboVal(_rfBox), _rfVal)) ComboSelectVal(_rfBox, _rfVal);" in m.group(0)
    assert 'Obj(root, "relay_findings")' in src                       # what the runner reports
    assert "PaintRelayFindingsInEffect(_lastRoot);" in src            # on every status render
    assert re.search(r'case "relay_findings":', src)                  # timing table
    assert 'SetAutomationId(_rfBox, "relayFindingsBox")' in src       # reachable by the UI driver
    block = src[src.index("UIElement RelayFindingsControl()"):src.index("void PaintRelayFindingsInEffect")]
    assert "File." not in block and "WriteAllText" not in block


def test_the_ui_driver_script_knows_the_box():
    src = _read("scripts", "win", "set_setting_via_ui.ps1")
    assert re.search(r"'relay_findings'\s*=\s*'relayFindingsBox'", src)


def test_the_tooltip_says_the_queue_and_merge_are_unchanged_in_both_languages():
    src = _read("ui", "EffortPolicy.cs")
    assert "the queue, prompts and merge are unchanged" in src
    assert "\u30ad\u30e5\u30fc\u3082\u30d7\u30ed\u30f3\u30d7\u30c8\u3082\u30de\u30fc\u30b8\u3082\u5909\u308f\u308a\u307e\u305b\u3093" in src


def test_the_live_record_redirect_isolates_the_shadow_log():
    # the shadow log must be redirected like every other .fleet writer, or the suite would
    # write to the operator's records (see conftest.py LIVE_RECORD_REDIRECTS).
    src = _read("conftest.py")
    assert '"relay.relay_findings": {"LOG": "relay_findings_shadow.jsonl"}' in src


def test_the_status_snapshot_carries_the_report(tmp_path, monkeypatch):
    p = tmp_path / "settings.txt"
    p.write_text("relay_findings=shadow\n", encoding="utf-8")
    RF._reset_for_test()
    monkeypatch.setattr("tools.settings_path.settings_file", lambda: str(p))
    assert FR._relay_findings_block() == {
        "relay_findings": {"mode": "shadow", "recorded": 0, "with_findings": 0}}
    p.write_text("relay_findings=off\n", encoding="utf-8")
    assert FR._relay_findings_block()["relay_findings"]["mode"] == "off"
    # on is reserved and reads back as on but behaves as off (no second stage anywhere)
    p.write_text("relay_findings=on\n", encoding="utf-8")
    assert FR._relay_findings_block()["relay_findings"]["mode"] == "on"
    RF._reset_for_test()


def test_the_snapshot_calls_the_report():
    assert "_snap.update(_relay_findings_block())" in _read("relay", "fleet_runner.py")


def test_the_cockpit_still_compiles_with_the_control():
    from bench import ui_build_check as B
    if not os.path.isfile(B.CSC):
        if os.environ.get("REQUIRE_CSC") == "1":
            pytest.fail("csc missing")
        pytest.skip("csc not available")
    import tempfile
    out = tempfile.mkdtemp(prefix="relay_findings_build_")
    try:
        for name, srcs in B.targets_from_rebuild_script():
            if name != "FleetCockpit":
                continue
            r = B.build(name, srcs, out)
            assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    finally:
        shutil.rmtree(out, ignore_errors=True)
