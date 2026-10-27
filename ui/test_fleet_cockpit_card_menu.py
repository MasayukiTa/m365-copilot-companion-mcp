"""Source-level regression checks for the per-card "⋮" (More actions) kebab.

The kebab used to appear only on completed/released cards; it now appears on EVERY card
(running / waiting / stopped / runtime-wait / completed). Its items are built in ONE place --
``BuildCardMenu`` (a state -> items table) -- so adding an action is one row there rather than a
new branch in ``Card()``. These checks pin that single-table shape and the lazy construction, so
a refactor cannot quietly split the menu back out per-state or make every card build its menu
eagerly.
"""
from tools import cockpit_source  # reads every part of the split cockpit source


SOURCE = cockpit_source.read(encoding="utf-8")


def _build_card_menu():
    """The body of BuildCardMenu -- the one place state maps to menu items."""
    return cockpit_source.member(SOURCE, "CardMenuModel BuildCardMenu")


def test_kebab_click_is_not_consumed_before_button_click():
    body = SOURCE[SOURCE.index("UIElement CardKebabBtn"):]
    body = body[: body.index("\n    }")]
    assert "btn.Click +=" in body
    assert "btn.PreviewMouseLeftButtonUp" not in body


def test_kebab_menu_is_shifted_inside_the_fleet_window():
    assert "cm.HorizontalOffset = -250;" in SOURCE


def test_completed_card_menu_has_reuse_and_archive_actions():
    # The completed branch of the one table keeps the reuse actions then, after a separator,
    # the archive action. Order is pinned so the completed card is unchanged by the running work.
    body = _build_card_menu()
    done = body[body.index("if (finishedCard)"):]
    done = done[: done.index("// Active card")]
    assert 'T("copy_result")' in done
    assert 'T("reveal_artifacts")' in done
    assert 'T("rerun_same")' in done
    assert "m.Separator();" in done
    assert 'T("to_history")' in done
    assert "ShowScaleToast(RevealPath(firstArtifact)" in done
    # copy_result (reuse) must come before to_history (archive/cleanup)
    assert done.index('T("copy_result")') < done.index('T("to_history")')


def test_every_card_gets_the_functional_kebab_not_a_released_label():
    # The kebab is added for every card from the one table; the old closed||terminal-only branch
    # and the always-visible release button are gone. A stub "released"-only menu must never return.
    assert "var menu = BuildCardMenu(" in SOURCE
    assert "right.Children.Add(CardKebabBtn(menu.Labels.ToArray()" in SOURCE
    assert 'CardKebabBtn(new string[] { T("released") }' not in SOURCE


def test_the_menu_is_one_table_not_a_branch_per_state():
    # Exactly one method maps state to items. If a second BuildCardMenu-like builder appears, the
    # "one place" invariant is broken and this should fail so it is noticed.
    assert SOURCE.count("CardMenuModel BuildCardMenu") == 1
