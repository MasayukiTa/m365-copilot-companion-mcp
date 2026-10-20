# -*- coding: utf-8 -*-
"""`org_ab` agrees in every layer, is reachable from the GUI, and is shadow only.

Owner rule: a setting with no GUI path does not exist. The registry (tools/settings_keys.py), the
reader (relay/org/ab.py) and the cockpit (ui/EffortPolicy.cs and the cockpit partial classes) are
compared here, the way tests/test_org_shadow_eval_agrees.py compares the shadow evaluation.

Unlike its two siblings (org_version_record, org_shadow_eval), org_ab's reader re-reads the
setting at the start of every run, so the registry must declare it EACH_GATE, and the cockpit
timing switch must group it with the each_gate keys, not the sweep_start ones.
"""
from __future__ import annotations

import io
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.org import ab as AB  # noqa: E402
from tools import cockpit_source  # noqa: E402  (the cockpit source is split across files)
from tools import settings_keys as SK  # noqa: E402


def _read(*parts):
    with io.open(os.path.join(REPO, *parts), encoding="utf-8-sig") as fh:
        return fh.read()


def test_the_registry_declares_it_each_gate_default_off():
    # org_ab is read at the start of every run, so it takes effect each gate, not sweep_start.
    assert SK.effect("org_ab") == SK.EACH_GATE
    assert SK.default("org_ab") == "off"
    assert AB.KEY == "org_ab" and AB.MODES == ("off", "shadow")


def test_the_cockpit_names_the_same_key_modes_and_default():
    src = _read("ui", "EffortPolicy.cs")
    m = re.search(r'class OrgAbView.*?public const string Key = "([a-z_]+)";\s*public const '
                  r'string Default = "([a-z]+)";\s*public static readonly string\[\] Modes = '
                  r'\{([^}]*)\};', src, re.S)
    assert m, "OrgAbView not found"
    assert m.group(1) == AB.KEY and m.group(2) == SK.default("org_ab")
    assert tuple(re.findall(r'"([a-z]+)"', m.group(3))) == AB.MODES
    assert '"on"' not in m.group(3)                      # `on` is not implemented, so not offered


def test_the_popup_has_the_control_the_parser_and_the_each_gate_timing_word():
    src = cockpit_source.read(encoding="utf-8-sig")
    assert "col.Children.Add(OrgAbControl());" in src
    assert "SaveKey(OrgAbView.Key" in src
    assert 'ln.StartsWith("org_ab=")' in src
    assert src.count('case "org_ab":') == 1
    # org_ab must sit in the each_gate arm, immediately after org_proposals (its sibling that
    # also re-reads per run) -- NOT in the sweep_start arm next to org_shadow_eval.
    assert re.search(r'case "org_proposals":\s*case "org_ab":', src)
    assert not re.search(r'case "org_shadow_eval":\s*case "org_ab":', src)
    assert "orgAbBox" in _read("scripts", "win", "set_setting_via_ui.ps1")


def test_no_layer_offers_a_value_that_changes_a_prompt():
    note = SK.KEYS["org_ab"].note
    assert "No prompt or admission changes either way" in note
    assert "off writes nothing" in note
    assert "Takes effect on the NEXT run" in note
