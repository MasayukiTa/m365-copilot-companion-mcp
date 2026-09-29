# -*- coding: utf-8 -*-
"""The left FleetCockpit spine is task CONTENT, not a second execution timeline."""
from pathlib import Path

SRC = Path(__file__).with_name("FleetCockpit.cs").read_text(encoding="utf-8-sig")


def _spine():
    i = SRC.index("UIElement BuildSpineContent(")
    # The next helper after this method is stable enough for a source-contract boundary.
    j = SRC.index("\n    ", i + 100)
    # Find the next method signature after the BuildSpineContent body by looking for the known
    # expanded-card timeline helper, which must remain separate.
    k = SRC.index("List<Tuple<string, string>> BuildTimelineEvents", i)
    return SRC[i:k]


def test_left_spine_is_content_details_not_execution_timeline():
    b = _spine()
    assert '"内容詳細" : "Content details"' in b
    assert '"Execution timeline"' not in b
    assert 'Theme.TimelineLabel(' not in b
    assert 'Theme.TimelineColor(' not in b


def test_content_details_follows_the_worker_being_inspected():
    b = _spine()
    assert "SpineFocusWorker(workers)" in b
    assert 'S(primaryWorker, "goal_summary")' in b
    assert 'S(primaryWorker, "goal")' in b, "old snapshots still need a full-goal fallback"
    assert 'S(primaryWorker, "status")' in b
    assert 'I(primaryWorker, "turn")' in b
    assert 'S(primaryWorker, "reason")' in b


def test_content_details_surfaces_durable_execution_state_when_present():
    b = _spine()
    assert 'Obj(primaryWorker, "execution")' in b
    for key in ("state", "current_step", "last_progress", "next_step", "waiting_reason"):
        assert f'S(execution, "{key}")' in b
    assert 'execution.TryGetValue("artifacts"' in b


def test_spine_repaint_signature_tracks_content_not_phase_event_count():
    assert "string SpineDetailSignature(Dictionary<string, object> w)" in SRC
    refresh = SRC[SRC.index("string SpineDetailSignature(Dictionary<string, object> w)"):]
    refresh = refresh[:refresh.index("\n    void ", 20)] if "\n    void " in refresh[20:] else refresh[:3000]
    for token in ('"goal_summary"', '"reason"', '"current_step"', '"last_progress"', '"next_step"', '"waiting_reason"'):
        assert token in refresh
    around = SRC[SRC.index("string spineSig =") - 1200:SRC.index("string spineSig =") + 800]
    assert "SpineDetailSignature(primaryW)" in around
    assert "primaryPhaseCount" not in around


def test_expanded_card_keeps_timeline_evidence():
    # Requirement change is presentation-only for the left spine. Historical phase evidence stays
    # available in the expanded task view.
    assert 'SectLabel(_lang == 0 ? "タイムライン" : "Timeline")' in SRC
    assert "List<Tuple<string, string>> BuildTimelineEvents" in SRC
    timeline = SRC[SRC.index("List<Tuple<string, string>> BuildTimelineEvents"):]
    assert "Theme.TimelineColor(peEvent, _dark)" in timeline
    assert "Theme.TimelineLabel(peEvent, _lang)" in timeline
