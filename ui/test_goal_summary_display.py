# -*- coding: utf-8 -*-
"""The cockpit shows compact task identities while retaining full goals in the expanded view."""
from pathlib import Path

COCKPIT = Path(__file__).with_name("FleetCockpit.cs")


def src():
    return COCKPIT.read_text(encoding="utf-8-sig")


def test_directive_band_lists_one_summary_per_distinct_full_goal():
    s = src()
    i = s.index("UIElement DirectiveBand(")
    b = s[i:s.index("\n    //", i + 1000)]
    assert 'string fullGoal = S(tw, "goal")' in b
    assert 'string displayGoal = S(tw, "goal_summary")' in b
    assert 'goalDisplays.Add(' in b
    assert 'string.Join("\\n", goalDisplays.ToArray())' in b
    assert 'string.Join("\\n", goalTexts.ToArray())' not in b


def test_worker_card_prefers_goal_summary_for_headline():
    s = src()
    i = s.index("Border Card(Dictionary<string, object> w)")
    b = s[i:i + 9000]
    assert 'string goalSummary = S(w, "goal_summary")' in b
    assert 'string headline = !string.IsNullOrEmpty(goalSummary)' in b
    assert 'CardTitle(convTitle, goal)' in b  # old-history / old-snapshot fallback remains


def test_expanded_overview_still_receives_full_goal():
    s = src()
    assert "BuildCardTabs(w, name, goal, last, reason, terminal)" in s
    assert "sp.Children.Add(RoText(goal, Muted, 12.5));" in s


def test_history_rows_prefer_persisted_goal_summary_with_old_row_fallback():
    s = src()
    i = s.index("Border HistoryRow(")
    b = s[i:i + 4500]
    assert 'string histSummary = S(e, "goal_summary")' in b
    assert "CardTitle(S(e, \"conv_title\"), S(e, \"goal\"))" in b


def test_both_history_archive_routes_persist_goal_summary():
    s = src()
    assert s.count('e["goal_summary"] = S(w, "goal_summary");') == 2
