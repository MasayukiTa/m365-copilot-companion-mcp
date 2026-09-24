# -*- coding: utf-8 -*-
"""WPF font-fallback chain lists Japanese fonts first (owner-reported bug, 2026-09-24).

Same bug class as the WinForms fix (scripts/win/ui_font.ps1): a FontFamily string that has no
Japanese-capable font lets WPF's per-character fallback hit a Chinese font on a non-Japanese
system locale. This is a lightweight source-level check (a constant string value), consistent
with how this repo checks other C# constants without a full WPF harness -- see
bench/ui_build_check.py for the heavier compile-level gate."""
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THEME_CS = os.path.join(REPO, "ui", "Theme.cs")
COPILOT_CHAT_CS = os.path.join(REPO, "ui", "CopilotChat.cs")


def test_theme_ui_font_lists_a_japanese_font_first():
    text = open(THEME_CS, encoding="utf-8").read()
    value = text.split("UiFont", 1)[1].split(";", 1)[0]
    assert "Yu Gothic UI" in value, value
    # Japanese font must come before the old Segoe UI Variable fallback, not just be present
    # anywhere in the chain -- WPF tries fonts left-to-right.
    assert value.find("Yu Gothic UI") < value.find("Segoe UI Variable"), value


def test_copilot_chat_has_no_duplicate_hardcoded_font_chain():
    # Guards against the duplicate-hardcode regressing: both BODY_FACE and the user-bubble
    # TextBox must reference Theme.UiFont instead of re-hardcoding the font string.
    text = open(COPILOT_CHAT_CS, encoding="utf-8").read()
    # Look only at live code, not a historical comment (~line 4580) that mentions the old string
    # by name while explaining a past bug -- strip '//' line comments first.
    code_lines = [line.split("//", 1)[0] for line in text.splitlines()]
    code = "\n".join(code_lines)
    assert '"Segoe UI Variable, Segoe UI"' not in code, (
        "ui/CopilotChat.cs still hardcodes the old font-chain string instead of using Theme.UiFont"
    )
