"""Source-level regression checks for the running card's top-priority (boost) kebab action."""
from tools import cockpit_source  # reads every part of the split cockpit source


SOURCE = cockpit_source.read(encoding="utf-8")


def _card_body():
    """The body of Border Card(...), where the running card's controls are built."""
    return cockpit_source.member(SOURCE, "CardMenuModel BuildCardMenu(")


def test_running_card_has_a_boost_kebab_action():
    body = _card_body()
    # The boost control is gated behind the feature flag and uses the same kebab helper as the
    # completed card, and it must actually send the fleet command -- not just show a toast.
    assert "_priorityBoostEnabled" in body
    assert "CardKebabBtn(" in cockpit_source.member(SOURCE, "Border Card(Dictionary<string, object> w)")
    assert 'T("boost_set")' in body
    assert 'T("boost_clear")' in body
    assert "RequestSetPriorityBoost(nmb, !on)" in body
    assert "/*SABOTAGE*/" not in body


def test_boost_action_confirms_with_a_toast():
    body = _card_body()
    assert 'ShowScaleToast(on ? T("boost_clear_done") : T("boost_set_done"))' in body
