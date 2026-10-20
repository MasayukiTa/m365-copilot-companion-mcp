# -*- coding: utf-8 -*-
"""`org_proposals` agrees in every layer and is reachable from the GUI.

Owner rule: a setting with no GUI path does not exist. The registry (tools/settings_keys.py), the
reader (relay/org/proposals.py) and the cockpit (ui/EffortPolicy.cs and the cockpit partial
classes) are compared here, the way tests/test_org_version_record_agrees.py compares the version
record.
"""
from __future__ import annotations

import io
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.org import proposals as P  # noqa: E402
from tools import cockpit_source  # noqa: E402  (the cockpit source is split across files)
from tools import settings_keys as SK  # noqa: E402


def _read(*parts):
    with io.open(os.path.join(REPO, *parts), encoding="utf-8-sig") as fh:
        return fh.read()


def test_the_registry_declares_it_each_gate_default_off():
    assert SK.effect("org_proposals") == SK.EACH_GATE
    assert SK.default("org_proposals") == "off"
    assert P.KEY == "org_proposals" and P.MODES == ("off", "on")


def test_the_cockpit_names_the_same_key_modes_and_default():
    src = _read("ui", "EffortPolicy.cs")
    m = re.search(r'class OrgProposalView.*?public const string Key = "([a-z_]+)";\s*public const '
                  r'string Default = "([a-z]+)";\s*public static readonly string\[\] Modes = '
                  r'\{([^}]*)\};', src, re.S)
    assert m, "OrgProposalView not found"
    assert m.group(1) == P.KEY and m.group(2) == SK.default("org_proposals")
    assert tuple(re.findall(r'"([a-z]+)"', m.group(3))) == P.MODES


def test_the_popup_has_the_control_the_parser_and_the_timing_word():
    src = cockpit_source.read(encoding="utf-8-sig")
    assert "col.Children.Add(OrgProposalControl());" in src
    assert "SaveKey(OrgProposalView.Key" in src
    assert 'ln.StartsWith("org_proposals=")' in src
    # in the each_gate group, beside team_mode, ahead of the contiguous fan-out budget cases
    assert re.search(r'case "team_mode":\s*case "org_proposals":\s*case "fanout_max_depth":', src)
    assert src.count('case "org_proposals":') == 1
    assert "orgProposalBox" in _read("scripts", "win", "set_setting_via_ui.ps1")


def test_no_layer_offers_a_value_that_changes_a_prompt():
    note = SK.KEYS["org_proposals"].note
    assert "No prompt or admission changes either way" in note
    assert "applies nothing" in note
