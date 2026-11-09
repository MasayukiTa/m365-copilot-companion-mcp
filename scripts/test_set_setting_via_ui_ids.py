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
    for view_key in ("effort_policy", "fanout", "tool_transport", "resident_tools", "merge_conversation",
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


def _steppers() -> dict:
    """key -> {Value,Dec,Inc} AutomationIds from the $Steppers map in the driver.

    These settings are numeric steppers, not combo boxes; the driver clicks -/+ until the value
    label shows the target, so they are kept out of $Boxes and have their own map."""
    text = SCRIPT.read_text(encoding="utf-8")
    block = text.split("$Steppers = @{", 1)[1].split("\n}\n", 1)[0]
    out = {}
    for key, inner in re.findall(r"'([a-z_]+)'\s*=\s*@\{([^}]*)\}", block):
        ids = dict(re.findall(r"(Value|Dec|Inc)\s*=\s*'(\w+)'", inner))
        out[key] = ids
    return out


def test_every_driven_stepper_has_its_three_automation_ids_in_the_cockpit():
    cs = cockpit_source.read(encoding="utf-8")
    steppers = _steppers()
    assert steppers, "no steppers are listed in the script"
    for key, ids in steppers.items():
        assert set(ids) == {"Value", "Dec", "Inc"}, (key, ids)
        for role, aid in ids.items():
            assert 'SetAutomationId(' in cs and '"%s"' % aid in cs, (key, role, aid)


def test_autoscale_max_and_maxtabs_are_driven_as_steppers():
    steppers = _steppers()
    assert "autoscale_max" in steppers, "autoscale_max must be drivable"
    assert "maxtabs" in steppers, "maxtabs must be drivable"


def test_every_driven_stepper_key_is_a_declared_setting():
    from tools.settings_keys import KEYS
    names = set(KEYS)
    for key in _steppers():
        assert key in names, key


def test_steppers_and_combos_are_disjoint_and_ids_are_unique():
    boxes = _boxes()
    steppers = _steppers()
    assert not (set(boxes) & set(steppers)), set(boxes) & set(steppers)
    all_ids = list(boxes.values())
    for ids in steppers.values():
        all_ids += list(ids.values())
    assert len(all_ids) == len(set(all_ids)), "an AutomationId is used twice"


def test_a_stepper_is_set_through_a_read_back_and_failures_exit_nonzero():
    text = SCRIPT.read_text(encoding="ascii")
    ok = text.index("via the settings stepper (read back)")
    # the success line is reached only after settings.txt was consulted for the same key
    assert text.rindex("Read-SettingsFile", 0, ok) < ok
    # the stepper value label is re-read (the stopped-at check) before success is printed
    assert "the stepper stopped at" in text
    assert text.count("via the settings stepper") == 1
