"""The setting driver finds each combo by an AutomationId that the cockpit must really set."""
from __future__ import annotations

import re
from pathlib import Path

from tools import cockpit_source  # the cockpit source is split across partial-class files

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "win" / "set_setting_via_ui.ps1"


def _boxes() -> dict:
    text = SCRIPT.read_text(encoding="utf-8")
    block = text.split("$Boxes = @{", 1)[1].split("}", 1)[0]
    return dict(re.findall(r"'([a-z_]+)'\s*=\s*'(\w+)'", block))


def test_every_driven_combo_has_its_automation_id_in_the_cockpit():
    cs = cockpit_source.read(encoding="utf-8")
    boxes = _boxes()
    assert boxes, "no settings are listed in the script"
    for key, aid in boxes.items():
        assert 'SetAutomationId(' in cs and '"%s"' % aid in cs, (key, aid)
    assert '"settingsButton"' in cs


def test_every_driven_key_is_a_declared_setting():
    from tools.settings_keys import KEYS
    names = set(KEYS)
    for key in _boxes():
        assert key in names, key


def test_the_script_is_ascii_and_never_writes_settings_directly():
    raw = SCRIPT.read_bytes()
    raw.decode("ascii")
    low = raw.decode("ascii").lower()
    assert "set-content" not in low and "out-file" not in low


def _declared_ids() -> dict:
    """Every combo AutomationId the cockpit source sets."""
    cs = cockpit_source.read(encoding="utf-8")
    return set(re.findall(r'SetAutomationId\(\w+,\s*"(\w+Box)"\)', cs))


def test_the_script_covers_every_combo_id_the_cockpit_declares():
    declared = _declared_ids()
    driven = set(_boxes().values())
    assert declared, "no combo AutomationIds found in the cockpit source"
    assert declared <= driven, sorted(declared - driven)


def test_every_settings_combo_has_an_id_and_is_driven():
    # each settings-popup combo is paired with a *View class whose Key it saves
    cs = cockpit_source.read(encoding="utf-8")
    boxes = _boxes()
    for view_key in ("effort_policy", "fanout", "tool_transport", "merge_conversation",
                     "fleet_auto_resume", "supervisor_self_restart", "tool_probe_idle_min",
                     "fanout_load_routing", "team_mode", "agent_board_mode",
                     "org_version_record", "org_proposals", "org_shadow_eval",
                     "fanout_max_depth", "fanout_hierarchical_merge", "fanout_write_scope"):
        assert view_key in boxes, view_key
    assert len(set(boxes.values())) == len(boxes)
    for aid in boxes.values():
        assert cs.count('"%s")' % aid) == 1, aid


def test_success_line_only_after_a_read_back_and_failures_exit_nonzero():
    text = SCRIPT.read_text(encoding="ascii")
    ok = text.index("through the cockpit (read back)")
    assert text.rindex("Read-Combo", 0, ok) < ok
    assert "Read-SettingsFile" in text
    # the combo found after the pick is null-checked before it is read (the InvokeMethodOnNull)
    assert "if ($null -ne $c2) { $after = Read-Combo $c2 }" in text
    assert "if ($null -eq $combo) { throw" in text
    assert 'Write-Output ("FAILED:' in text and "exit 1" in text
    # nothing prints a success line outside the final read-back
    assert text.count("through the cockpit") == 1
