# -*- coding: utf-8 -*-
"""`org_shadow_eval` agrees in every layer, is reachable from the GUI, and is shadow only.

Owner rule: a setting with no GUI path does not exist. The registry (tools/settings_keys.py), the
reader (relay/org/shadow_eval.py) and the cockpit (ui/EffortPolicy.cs and the cockpit partial
classes) are compared here, the way tests/test_org_proposals_agrees.py compares the proposals.
"""
from __future__ import annotations

import io
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.org import shadow_eval as SE  # noqa: E402
from tools import cockpit_source  # noqa: E402  (the cockpit source is split across files)
from tools import settings_keys as SK  # noqa: E402


def _read(*parts):
    with io.open(os.path.join(REPO, *parts), encoding="utf-8-sig") as fh:
        return fh.read()


def test_the_registry_declares_it_sweep_start_default_off():
    assert SK.effect("org_shadow_eval") == SK.SWEEP_START
    assert SK.default("org_shadow_eval") == "off"
    assert SE.KEY == "org_shadow_eval" and SE.MODES == ("off", "shadow")


def test_the_cockpit_names_the_same_key_modes_and_default():
    src = _read("ui", "EffortPolicy.cs")
    m = re.search(r'class OrgShadowView.*?public const string Key = "([a-z_]+)";\s*public const '
                  r'string Default = "([a-z]+)";\s*public static readonly string\[\] Modes = '
                  r'\{([^}]*)\};', src, re.S)
    assert m, "OrgShadowView not found"
    assert m.group(1) == SE.KEY and m.group(2) == SK.default("org_shadow_eval")
    assert tuple(re.findall(r'"([a-z]+)"', m.group(3))) == SE.MODES
    assert '"on"' not in m.group(3)                      # `on` is not implemented, so not offered


def test_the_popup_has_the_control_the_parser_and_the_timing_word():
    src = cockpit_source.read(encoding="utf-8-sig")
    assert "col.Children.Add(OrgShadowControl());" in src
    assert "SaveKey(OrgShadowView.Key" in src
    assert 'ln.StartsWith("org_shadow_eval=")' in src
    assert src.count('case "org_shadow_eval":') == 1
    assert re.search(r'case "agent_board_mode":\s*case "org_shadow_eval":\s*case "org_version_record":',
                     src)
    assert "orgShadowBox" in _read("scripts", "win", "set_setting_via_ui.ps1")


def test_no_layer_offers_a_value_that_changes_a_prompt():
    note = SK.KEYS["org_shadow_eval"].note
    assert "No prompt or admission changes either way" in note
    assert "cannot show a proposal is better" in note
    assert "`on` is not implemented" in note
