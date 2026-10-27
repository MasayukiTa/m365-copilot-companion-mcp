# -*- coding: utf-8 -*-
"""Source-level checks for the cockpit's "make this top priority" control (range 4/6).

## The requirement (owner, 2026-10-27 16:16)

With ~330 tasks queued, an operator wants to hand-raise one task to the very top. This range
owns the COCKPIT UI surface of that feature:

  (1) a per-card "make top priority / clear" action (kebab) on running cards,
  (2) a marker on boosted cards,
  (3) a boosted-count line in the cockpit,
  (4) a warning when that count passes the dilution threshold, and
  (6) a one-line "why isn't this progressing" explanation on a stalled card.

The backend that APPLIES a boost (queue head, next-turn precedence, audit) is a different
range and is pinned by ui/test_priority_boost_persists_and_audits.py etc. Here we assert the
COCKPIT SOURCE wires the control to that backend and shows the marks/count/warning/why-line --
the layer test_fleet_cockpit_card_menu.py covers for the completed-card menu, applied to boost.

These read the WHOLE split cockpit source via tools.cockpit_source so a method moved into any
FleetCockpit.*.cs part is still seen (an `assert ... in src` would otherwise pass/fail for the
wrong reason -- see that module's own docstring).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import cockpit_source  # noqa: E402  # reads every FleetCockpit*.cs part

# Read THIS checkout's cockpit parts, not whichever `tools` package happens to win on sys.path.
# In an isolated clone the ambient PYTHONPATH can pin tools.cockpit_source to the production
# checkout, whose REPO/UI then points outside this clone; pinning ui_dir to the ui/ beside this
# test file keeps the assertions measuring the source actually under test.
_UI_DIR = os.path.dirname(os.path.abspath(__file__))

SOURCE = cockpit_source.read(encoding="utf-8", ui_dir=_UI_DIR)


# ── the source really is the concatenation of all parts, not just FleetCockpit.cs ───────────

def test_reads_more_than_one_part():
    parts = cockpit_source.paths(ui_dir=_UI_DIR)
    assert len(parts) > 1
    assert any(p.endswith("FleetCockpit.Card.cs") for p in parts)
    assert any(p.endswith("FleetCockpit.CardToolbar.cs") for p in parts)


# ── (1) per-card kebab action to set AND clear the boost, wired to the fleet command ─────────

def test_running_card_has_a_boost_kebab_action():
    # The action is only added on running (non-terminal) cards and only when the feature is on.
    assert "_priorityBoostEnabled" in SOURCE
    # Both the set and the clear label are offered (toggles on current state).
    assert 'T("boost_set")' in SOURCE
    assert 'T("boost_clear")' in SOURCE
    # The action sends the one fleet command that owns applying/auditing the boost, toggling
    # on the card's CURRENT boost state. This is the call SITE, not merely the method existing.
    assert "RequestSetPriorityBoost(nmb, !on)" in SOURCE
    # It is presented through the same kebab the completed-card menu uses.
    assert "CardKebabBtn(" in SOURCE


def test_boost_action_confirms_with_a_toast():
    assert 'T("boost_set_done")' in SOURCE
    assert 'T("boost_clear_done")' in SOURCE
    assert "ShowScaleToast(" in SOURCE


# ── (2) boosted cards carry a visible marker ────────────────────────────────────────────────

def test_boosted_card_shows_a_marker():
    assert "IsBoosted(" in SOURCE
    assert 'T("boost_mark")' in SOURCE


# ── (3) the cockpit shows how many are boosted, over the whole board ─────────────────────────

def test_cockpit_shows_the_boosted_count():
    assert 'T("boost_count")' in SOURCE
    # Counted across the whole board, not just the goals gathered for the current view.
    assert "PriorityBoostCount(_toolbarAll)" in SOURCE


# ── (4) and warns when the count passes the dilution threshold ───────────────────────────────

def test_over_threshold_warning():
    assert "PriorityBoostThreshold(" in SOURCE
    assert 'T("boost_too_many")' in SOURCE
    # The warning is a danger-coloured line, not the normal muted one.
    assert "Theme.Danger(_dark)" in SOURCE


# ── (6) a stalled card explains why it is not progressing, in one line ───────────────────────

def test_stalled_card_says_why():
    # The helper surfaces queue rank / goals ahead / resource wait / the fleet's waiting_reason.
    assert "WhyNotProgressing(" in SOURCE
    for field in ("queue_rank", "queue_ahead", "resource_wait", "waiting_reason"):
        assert field in SOURCE, field


# ── the gear-popup toggle that gates the whole surface exists and persists ───────────────────

def test_settings_toggle_exists_and_persists():
    assert "PaintPriorityBoostBtn(" in SOURCE
    assert 'T("boost_setting")' in SOURCE
    assert "priority_boost_ui" in SOURCE  # persisted key in the shared settings.txt


# ── every boost string key the UI asks for is actually defined (no empty labels) ─────────────

def test_all_boost_string_keys_are_defined():
    strings = ""
    for p in cockpit_source.paths(ui_dir=_UI_DIR):
        if p.endswith("FleetCockpit.Strings.cs"):
            strings = open(p, encoding="utf-8").read()
            break
    assert strings, "FleetCockpit.Strings.cs not found among the cockpit parts"
    for key in ("boost_setting", "boost_set", "boost_clear", "boost_set_done",
                "boost_clear_done", "boost_mark", "boost_count", "boost_too_many"):
        assert ('k == "%s"' % key) in strings, key
