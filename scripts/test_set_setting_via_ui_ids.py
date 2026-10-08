"""The setting driver finds each combo by an AutomationId that the cockpit must really set."""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "win" / "set_setting_via_ui.ps1"
COCKPIT = REPO / "ui" / "FleetCockpit.cs"


def _boxes() -> dict:
    text = SCRIPT.read_text(encoding="utf-8")
    block = text.split("$Boxes = @{", 1)[1].split("}", 1)[0]
    return dict(re.findall(r"'([a-z_]+)'\s*=\s*'(\w+)'", block))


def test_every_driven_combo_has_its_automation_id_in_the_cockpit():
    cs = COCKPIT.read_text(encoding="utf-8")
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
