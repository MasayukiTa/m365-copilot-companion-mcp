# -*- coding: utf-8 -*-
"""The nested-split switch is a GUI-controlled setting, off by default, and never on in code.

A feature that can only be switched in code or on a command line does not exist for the
operator: `fanout_hierarchical_merge` is a settings key with a cockpit control. While it is off
the effective split depth is capped at 1 whatever `fanout_max_depth` says (the same behaviour as
before the key existed); on, the configured depth takes effect.
"""
from __future__ import annotations

import io
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay import fanout as fo               # noqa: E402
from relay import fleet_runner as FR         # noqa: E402
from tools import childproc                  # noqa: E402
from tools import settings_keys as SK        # noqa: E402

KEY = "fanout_hierarchical_merge"


def _read(*parts):
    with io.open(os.path.join(REPO, *parts), encoding="utf-8-sig") as fh:
        return fh.read()


def _settings(tmp_path, monkeypatch, *lines):
    p = tmp_path / "settings.txt"
    p.write_text("".join(ln + "\n" for ln in lines), encoding="utf-8")
    monkeypatch.setattr(FR, "_settings_path", lambda: str(p))
    monkeypatch.setattr(fo, "HIERARCHICAL_MERGE_READY", False)
    return p


# ---- registry ------------------------------------------------------------------------------

def test_the_key_is_declared_each_gate_default_off():
    assert SK.effect(KEY) == SK.EACH_GATE
    assert SK.default(KEY) == "off"
    assert fo.HIERARCHICAL_SETTING_KEY == KEY
    assert fo.HIERARCHICAL_SETTING_DEFAULT == "off"


# ---- behaviour -----------------------------------------------------------------------------

@pytest.mark.parametrize("configured", [1, 2, 3])
@pytest.mark.parametrize("extra", [[], [KEY + "=off"], [KEY + "=junk"], [KEY + "="]])
def test_off_or_absent_caps_the_depth_at_one(tmp_path, monkeypatch, configured, extra):
    _settings(tmp_path, monkeypatch, "fanout_max_depth=%d" % configured, *extra)
    assert fo.configured_max_depth() == configured
    assert fo.hierarchical_merge_setting() == "off"
    assert fo.effective_max_depth() == 1
    assert [d for d in range(4) if fo.may_split_at(d)] == [0]


@pytest.mark.parametrize("configured,allowed", [(1, [0]), (2, [0, 1]), (3, [0, 1, 2])])
def test_on_lets_the_configured_depth_take_effect(tmp_path, monkeypatch, configured, allowed):
    _settings(tmp_path, monkeypatch, "fanout_max_depth=%d" % configured, KEY + "=ON")
    assert fo.hierarchical_merge_setting() == "on"
    assert fo.effective_max_depth() == configured
    assert [d for d in range(5) if fo.may_split_at(d)] == allowed


def test_a_change_is_picked_up_without_a_restart(tmp_path, monkeypatch):
    p = _settings(tmp_path, monkeypatch, "fanout_max_depth=2", KEY + "=off")
    assert fo.effective_max_depth() == 1
    p.write_text("fanout_max_depth=2\n" + KEY + "=on\n", encoding="utf-8")
    assert fo.effective_max_depth() == 2
    p.write_text("fanout_max_depth=2\n", encoding="utf-8")
    assert fo.effective_max_depth() == 1


def test_the_status_report_names_the_setting(tmp_path, monkeypatch):
    p = _settings(tmp_path, monkeypatch, "fanout_max_depth=3")
    assert FR._fanout_depth_block() == {"fanout_depth": {
        "configured": 3, "effective": 1, "reason": "hierarchical merge setting is off",
        "hierarchical_merge": "off"}}
    p.write_text("fanout_max_depth=3\n" + KEY + "=on\n", encoding="utf-8")
    assert FR._fanout_depth_block() == {"fanout_depth": {
        "configured": 3, "effective": 3, "reason": "", "hierarchical_merge": "on"}}
    # on, but nothing asked for more than one level: nothing to explain
    p.write_text("fanout_max_depth=1\n" + KEY + "=on\n", encoding="utf-8")
    assert fo.depth_report()["reason"] == ""


def test_the_test_hook_still_forces_the_deeper_behaviour(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "fanout_max_depth=2")
    monkeypatch.setattr(fo, "HIERARCHICAL_MERGE_READY", True)
    assert fo.effective_max_depth() == 2


# ---- never on by default in production code -------------------------------------------------

def test_the_default_is_off_everywhere_and_no_code_seeds_it_on():
    assert fo.HIERARCHICAL_MERGE_READY is False
    assert fo.HIERARCHICAL_SETTING_DEFAULT == "off" and SK.default(KEY) == "off"
    cs = _read("ui", "EffortPolicy.cs")
    assert re.search(r'public const string Default = "off";', cs[cs.index("class HierarchicalMergeView"):])
    pat = re.compile(r"""["']fanout_hierarchical_merge\s*=\s*on""", re.I)
    bad = []
    out = childproc.run(["git", "ls-files"], cwd=REPO, check=True).stdout
    for rel in out.splitlines():
        if not rel.endswith((".ps1", ".bat", ".cmd", ".cs", ".py", ".txt", ".example", ".template")):
            continue
        base = os.path.basename(rel)
        if base.startswith("test_") or rel.startswith(("tests/", "docs/", "bench/")) or "/test_" in rel:
            continue
        try:
            txt = _read(*rel.split("/"))
        except Exception:
            continue
        for i, ln in enumerate(txt.splitlines(), 1):
            if pat.search(ln) and not ln.lstrip().startswith(("#", "//")):
                bad.append("%s:%d" % (rel, i))
    assert not bad, "production code writes fanout_hierarchical_merge=on: %s" % bad


# ---- parity with the cockpit ---------------------------------------------------------------

def test_the_cockpit_constants_equal_the_python_side():
    cs = _read("ui", "EffortPolicy.cs")
    body = cs[cs.index("class HierarchicalMergeView"):]
    assert 'public const string Key = "%s";' % fo.HIERARCHICAL_SETTING_KEY in body
    assert 'public const string Default = "%s";' % SK.default(KEY) in body
    m = re.search(r"public static readonly string\[\] Modes = \{([^}]*)\}", body)
    assert m and sorted(x.strip().strip('"') for x in m.group(1).split(",")) == ["off", "on"]


def test_the_cockpit_has_a_visible_control_saving_through_savekey_only():
    src = _read("ui", "FleetCockpit.cs")
    # lives in the gear popup (BuildSettingsPanel), NOT in the header
    assert "col.Children.Add(HierarchicalMergeControl());" in src
    assert "ctrls.Children.Add(HierarchicalMergeControl());" not in src
    assert src.index("FanoutDepthControl());") < src.index("HierarchicalMergeControl());")
    assert len(re.findall(r"SaveKey\(HierarchicalMergeView\.Key, _hmVal\)", src)) == 1
    m = re.search(r"UIElement HierarchicalMergeControl\(\).*?\n    }\n", src, re.S)
    assert m and "File." not in m.group(0)
    assert "if (!HierarchicalMergeView.IsMode(sel) || sel == _hmVal) return;" in m.group(0)
    m = re.search(r"void PaintHierarchicalMerge\(\).*?\n    }\n", src, re.S)
    assert m and "!Equals(ComboVal(_hmBox), _hmVal)" in m.group(0)
    assert "HierarchicalMergeView.ParseLine(ln)" in src          # settings load, validated
    m = re.search(r"void PaintHierarchicalMergeInEffect\(.*?\n    }\n", src, re.S)
    assert m and 'Obj(root, "fanout_depth")' in m.group(0)
    assert "PaintHierarchicalMergeInEffect(root);" in src
    # the timing switch names the key (each_gate; the shape test in tools/ checks the value)
    assert re.search(r'case "fanout_hierarchical_merge":', src)


def test_the_tooltip_says_what_on_does():
    cs = _read("ui", "EffortPolicy.cs")
    assert "On lets fan-out depth above 1 take effect; verify with small goals first." in cs
    assert "System.Windows" not in cs and "PresentationFramework" not in cs
