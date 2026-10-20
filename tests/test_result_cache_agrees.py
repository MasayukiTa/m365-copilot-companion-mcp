# -*- coding: utf-8 -*-
"""`result_cache` agrees in every layer, is reachable from the GUI, and is shadow only.

Owner rule: a setting with no GUI path does not exist. The registry (tools/settings_keys.py),
the reader (relay/result_cache.py) and the cockpit (ui/EffortPolicy.cs + the cockpit partial
classes) are compared here, the way tests/test_fanout_load_routing_agrees.py compares the
sibling load routing. There is deliberately no value that serves from the cache: the recorder
notes what a cache would have returned and nothing else.
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
from relay import result_cache as RC  # noqa: E402
from tools import cockpit_source  # noqa: E402  (the cockpit source is split across files)
from tools import settings_keys as SK  # noqa: E402


def _read(*parts):
    with io.open(os.path.join(REPO, *parts), encoding="utf-8-sig") as fh:
        return fh.read()


def test_the_registry_declares_it_each_gate_default_off():
    assert SK.effect("result_cache") == SK.EACH_GATE
    assert SK.default("result_cache") == "off"
    assert RC.KEY == "result_cache"
    assert RC.MODES == ("off", "shadow")


def test_the_cockpit_names_the_same_key_modes_and_default():
    src = _read("ui", "EffortPolicy.cs")
    m = re.search(r'class ResultCacheView.*?public const string Key = "([a-z_]+)";\s*public const '
                  r'string Default = "([a-z]+)";\s*public static readonly string\[\] Modes = '
                  r'\{([^}]*)\};', src, re.S)
    assert m, "ResultCacheView not found"
    assert m.group(1) == RC.KEY
    assert m.group(2) == SK.default("result_cache")
    assert tuple(re.findall(r'"([a-z]+)"', m.group(3))) == RC.MODES


def test_no_layer_offers_a_value_that_serves():
    src = _read("ui", "EffortPolicy.cs")
    view = src[src.index("class ResultCacheView"):]
    view = view[:view.index("\n}\n")]
    assert 'IsMode(string v) { return v == "off" || v == "shadow"; }' in view
    assert "serve" not in view.lower() and '"on"' not in view and '"enforce"' not in view
    note = SK.KEYS["result_cache"].note
    assert "nothing is served from a cache" in note and "no prompt or output changes" in note


def test_the_cockpit_loads_persists_paints_and_does_not_refire():
    src = cockpit_source.read(encoding="utf-8-sig")
    # lives in the gear popup (BuildSettingsPanel), NOT in the header
    assert "col.Children.Add(ResultCacheControl());" in src
    assert "ctrls.Children.Add(ResultCacheControl());" not in src
    assert "ResultCacheView.ParseLine(ln)" in src                    # settings load
    assert "SaveKey(ResultCacheView.Key, _rcVal)" in src             # persist, SaveKey only
    m = re.search(r"void PaintResultCache\(\).*?\n    }\n", src, re.S)
    assert m and "if (!Equals(ComboVal(_rcBox), _rcVal)) ComboSelectVal(_rcBox, _rcVal);" in m.group(0)
    assert re.search(r'case "result_cache":', src)                   # timing table
    assert 'SetAutomationId(_rcBox, "resultCacheBox")' in src        # reachable by the UI driver
    # the control writes the setting only through SaveKey: no direct file writes of its own
    block = src[src.index("UIElement ResultCacheControl()"):src.index("void PaintResultCache")]
    assert "File." not in block and "WriteAllText" not in block


def test_the_ui_driver_script_knows_the_box():
    src = _read("scripts", "win", "set_setting_via_ui.ps1")
    assert re.search(r"'result_cache'\s*=\s*'resultCacheBox'", src)


def test_the_tooltip_says_assignment_is_unchanged_in_both_languages():
    src = _read("ui", "EffortPolicy.cs")
    assert "Record only: assignment is unchanged." in src
    assert "記録のみ: 割当は変わりません" in src


def test_the_status_snapshot_carries_the_report(tmp_path, monkeypatch):
    p = tmp_path / "settings.txt"
    p.write_text("result_cache=shadow\n", encoding="utf-8")
    RC._reset_for_test()
    monkeypatch.setattr("tools.settings_path.settings_file", lambda: str(p))
    assert FR._result_cache_block() == {"result_cache":
                                        {"mode": "shadow", "seen": 0, "hits": 0, "differed": 0}}
    p.write_text("result_cache=off\n", encoding="utf-8")
    assert FR._result_cache_block()["result_cache"]["mode"] == "off"
    RC._reset_for_test()


def test_the_snapshot_calls_the_report():
    src = _read("relay", "fleet_runner.py")
    assert "_snap.update(_result_cache_block())" in src


def test_the_cockpit_still_compiles_with_the_control():
    from bench import ui_build_check as B
    if not os.path.isfile(B.CSC):
        if os.environ.get("REQUIRE_CSC") == "1":
            pytest.fail("csc missing")
        pytest.skip("csc not available")
    import tempfile
    out = tempfile.mkdtemp(prefix="rcache_build_")
    try:
        for name, srcs in B.targets_from_rebuild_script():
            if name != "FleetCockpit":
                continue
            r = B.build(name, srcs, out)
            assert r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    finally:
        shutil.rmtree(out, ignore_errors=True)
