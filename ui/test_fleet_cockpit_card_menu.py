"""Source-level regression checks for completed-card secondary actions."""
from pathlib import Path


SOURCE = (Path(__file__).with_name("FleetCockpit.cs")).read_text(encoding="utf-8")


def test_kebab_click_is_not_consumed_before_button_click():
    body = SOURCE[SOURCE.index("UIElement CardKebabBtn"):]
    body = body[: body.index("\n    }")]
    assert "btn.Click +=" in body
    assert "btn.PreviewMouseLeftButtonUp" not in body


def test_completed_card_menu_has_reuse_and_archive_actions():
    assert 'T("copy_result")' in SOURCE
    assert 'T("reveal_artifacts")' in SOURCE
    assert 'T("rerun_same")' in SOURCE
    assert 'menuLabels.Add(null)' in SOURCE
    assert 'T("to_history")' in SOURCE


def test_released_card_has_no_noop_kebab():
    assert 'CardKebabBtn(new string[] { T("released") }' not in SOURCE
