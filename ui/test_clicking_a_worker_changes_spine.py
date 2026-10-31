"""A card click selects the worker shown in the left detail and timeline."""
from tools import cockpit_source


def test_card_click_selects_and_immediately_refreshes_the_spine():
    src = cockpit_source.read()
    card = cockpit_source.member(src, "Border Card(Dictionary<string, object> w)")
    assert "PreviewMouseLeftButtonDown" in card
    assert "SelectSpineWorker(name)" in card
    select = cockpit_source.member(src, "void SelectSpineWorker(string name)")
    assert "_spineWorkerName = name" in select
    assert "RefreshSpine(_lastRoot, false)" in select


def test_selected_worker_wins_over_multiple_expanded_cards_and_missing_selection_falls_back():
    src = cockpit_source.read()
    focus = cockpit_source.member(src, "Dictionary<string, object> SpineFocusWorker(")
    assert '_spineWorkerName' in focus
    assert focus.index('_spineWorkerName') < focus.index('_expanded')
    assert 'return workers[0]' in focus
