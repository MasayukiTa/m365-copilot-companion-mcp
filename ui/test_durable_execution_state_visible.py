# -*- coding: utf-8 -*-
"""The cockpit must surface durable task progress, not only conversation turns."""
from pathlib import Path

SOURCE = Path(__file__).with_name("FleetCockpit.cs").read_text(encoding="utf-8")


def test_collapsed_card_reads_the_durable_execution_contract():
    for token in (
        'Obj(w, "execution")',
        '"current_step"',
        '"current_step_index"',
        '"completed_count"',
        '"last_progress_at"',
        '"artifacts"',
    ):
        assert token in SOURCE
    assert 'Text = stepPrefix + OneLine(currentStep)' in SOURCE
    assert 'meta.Append(" · ✓ ").Append(completed)' in SOURCE


def test_expanded_overview_shows_steps_waiting_and_artifacts():
    assert 'UIElement ExecutionOverview(Dictionary<string, object> w)' in SOURCE
    for token in ('"completed_steps"', '"waiting_reason"', '"next_step"', '"Artifacts"'):
        assert token in SOURCE
    assert 'UIElement execView = ExecutionOverview(w);' in SOURCE


def test_execution_progress_participates_in_row_diffing():
    assert 'sb.Append("|exec:")' in SOURCE
    assert 'StableShortHash(S(ex, "last_progress"))' in SOURCE
    assert 'S(ex, "last_progress_at")' in SOURCE
