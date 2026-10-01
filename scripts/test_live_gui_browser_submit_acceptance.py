# -*- coding: utf-8 -*-
from scripts.live_gui_browser_submit_acceptance import judge_question_texts, _fresh_idle_reason


def test_exactly_one_nonempty_marker_question_passes():
    got = judge_question_texts(["protocol ... R5LIVE-x ... actual task"], "R5LIVE-x")
    assert got["ok"] is True
    assert got["question_count"] == 1
    assert got["marker_question_count"] == 1
    assert got["empty_question_count"] == 0


def test_duplicate_user_turn_fails_even_if_both_are_nonempty():
    got = judge_question_texts(["R5LIVE-x first", "R5LIVE-x first"], "R5LIVE-x")
    assert got["ok"] is False
    assert got["question_count"] == 2
    assert got["marker_question_count"] == 2
    assert any("expected exactly 1" in r for r in got["reasons"])


def test_empty_extra_turn_fails_closed():
    got = judge_question_texts(["R5LIVE-x real", "   "], "R5LIVE-x")
    assert got["ok"] is False
    assert got["empty_question_count"] == 1


def test_wrong_or_missing_marker_fails():
    got = judge_question_texts(["some other turn"], "R5LIVE-x")
    assert got["ok"] is False
    assert got["marker_question_count"] == 0


def test_fresh_idle_refuses_router_handoff_gap(tmp_path):
    state = tmp_path / ".fleet"
    (state / "tasks" / "for_fleet").mkdir(parents=True)
    (state / "tasks" / "for_fleet" / "j1.json").write_text("{}", encoding="utf-8")
    assert "for_fleet" in _fresh_idle_reason(state)


def test_fresh_idle_refuses_live_command_gap(tmp_path):
    state = tmp_path / ".fleet"
    (state / "commands.d").mkdir(parents=True)
    (state / "commands.d" / "1.json").write_text("{}", encoding="utf-8")
    assert "commands.d" in _fresh_idle_reason(state)


def test_fresh_idle_refuses_active_marker_and_accepts_clean_state(tmp_path):
    state = tmp_path / ".fleet"
    state.mkdir()
    assert _fresh_idle_reason(state) == ""
    (state / "fleet_run_active.json").write_text("{}", encoding="utf-8")
    assert "fleet_run_active" in _fresh_idle_reason(state)


def test_live_wait_requires_a_stable_question_snapshot_before_returning():
    import inspect
    from scripts.live_gui_browser_submit_acceptance import _wait_visible_questions
    src = inspect.getsource(_wait_visible_questions)
    assert "settle_s" in src
    assert "stable_since" in src
    assert "last_signature" in src
