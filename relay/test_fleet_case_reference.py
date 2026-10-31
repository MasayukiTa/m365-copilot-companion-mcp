import pytest
from relay.relay_fleet import RelayWorker
from tools import search_ops

GOAL = "枝さん_679かすれ調査　これ調べて"


@pytest.mark.parametrize("scenario", ["duplicate", "missing", "timeout", "conflict"])
def test_unresolved_cases_raise_gate(tmp_path, monkeypatch, scenario):
    case = tmp_path / "枝さん_679かすれ調査"
    case.mkdir()
    if scenario != "missing":
        (case / "やりたいこと.txt").write_text("依頼", encoding="utf-8")
    if scenario == "duplicate":
        other = tmp_path / "other" / case.name
        other.mkdir(parents=True)
        (other / "やりたいこと.txt").write_text("別の依頼", encoding="utf-8")
    if scenario == "conflict":
        (tmp_path / (case.name + ".md")).write_text("別の依頼", encoding="utf-8")
    real = search_ops.case_reference_search
    monkeypatch.setattr(search_ops, "case_reference_search", lambda goal: real(
        goal, [tmp_path], timeout=0 if scenario == "timeout" else 3))
    worker = RelayWorker(GOAL, "case-test")
    assert worker.goal == GOAL
    questions = []
    monkeypatch.setattr(worker, "_raise_stuck_gate", lambda q, trigger: questions.append(q) or True)
    worker._begin_send()
    assert questions and worker._case_gate_pending
    assert worker.outcome != "DONE"


def test_resolved_reference_is_in_prompt_with_raw_goal(tmp_path, monkeypatch):
    case = tmp_path / "枝さん_679かすれ調査"
    case.mkdir()
    (case / "やりたいこと.txt").write_text("依頼の内容", encoding="utf-8")
    real = search_ops.case_reference_search
    monkeypatch.setattr(search_ops, "case_reference_search", lambda goal: real(goal, [tmp_path]))
    worker = RelayWorker(GOAL, "case-test")
    assert worker.goal == GOAL
    assert GOAL in worker.job and str(case / "やりたいこと.txt") in worker.job
    assert "依頼の内容" in worker.job and not worker._case_question


def test_incomplete_single_request_proceeds_with_notice(tmp_path, monkeypatch):
    case = tmp_path / "枝さん_679かすれ調査"
    case.mkdir()
    request = case / "やりたいこと.txt"
    request.write_text("依頼", encoding="utf-8")
    result = search_ops.case_reference_search(GOAL, [tmp_path])
    result["errors"].append("search timeout")
    result["truncated"] = True
    monkeypatch.setattr(search_ops, "case_reference_search", lambda goal: result)
    worker = RelayWorker(GOAL, "case-test")
    assert worker.goal == GOAL
    assert not worker._case_question
    assert str(request) in worker.job
    assert "Search incomplete; other candidates may exist." in worker.job


def test_timeout_without_candidates_remains_error_and_fallback_has_guidance(tmp_path, monkeypatch):
    result = search_ops.case_reference_search(GOAL, [tmp_path], timeout=0)
    monkeypatch.setattr(search_ops, "case_reference_search", lambda goal: result)
    worker = RelayWorker(GOAL, "case-test")
    assert "search timeout" in worker._case_question
    monkeypatch.setattr(worker, "_raise_stuck_gate", lambda q, trigger: False)
    worker._begin_send()
    assert worker.outcome == "STUCK"
    assert "search timeout" in worker.reason
    assert "依頼ファイルの正確なフルパス" in worker.reason
    assert worker.goal == GOAL


def test_gate_failure_lists_folder_without_excerpt(tmp_path, monkeypatch):
    case = tmp_path / "枝さん_679かすれ調査"
    case.mkdir()
    real = search_ops.case_reference_search
    monkeypatch.setattr(search_ops, "case_reference_search", lambda goal: real(goal, [tmp_path]))
    worker = RelayWorker(GOAL, "case-test")
    monkeypatch.setattr(worker, "_raise_stuck_gate", lambda q, trigger: False)
    worker._begin_send()
    assert str(case) in worker.reason
    assert "依頼ファイルの正確なフルパス" in worker.reason
