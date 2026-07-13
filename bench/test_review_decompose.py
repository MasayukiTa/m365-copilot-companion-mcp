import json

from bench.review_decompose import (
    SUBTASKS_BEGIN,
    SUBTASKS_END,
    build_child_envelopes,
    parse_subtasks,
    validate_subtasks,
)
from relay.review_resilience import TaskEnvelope


def _parent():
    return TaskEnvelope(
        "p1", None, "c1", "producer", "parent goal", "C:/repo", depth=0,
        metadata={
            "scope": ["a.py", "b.py"],
            "authorization_preamble": "AUTHORIZED\n",
            "prohibited_actions": ["no edits"],
            "resilience_profile": "review",
        },
    )


def test_parse_validate_and_build_children_preserves_contract():
    body = [{
        "title": "input path",
        "objective": "Inspect input validation",
        "files": ["a.py"],
        "expected_evidence": ["line reference"],
        "output_contract": "FINDINGS",
        "reason_for_split": "separate input path",
    }]
    text = SUBTASKS_BEGIN + "\n" + json.dumps(body) + "\n" + SUBTASKS_END
    parsed, errors = parse_subtasks(text)
    assert errors == 0
    valid, validation_errors = validate_subtasks(_parent(), parsed, 8)
    assert validation_errors == []
    children = build_child_envelopes(_parent(), valid)
    assert len(children) == 1
    child = children[0]
    assert child.depth == 1
    assert child.parent_task_id == "p1"
    assert child.metadata["scope"] == ["a.py"]
    assert child.metadata["prohibited_actions"] == ["no edits"]
    assert child.goal_text.startswith("AUTHORIZED\n")
    assert "<<<FINDINGS>>>" in child.goal_text


def test_validation_rejects_scope_expansion_empty_and_duplicates():
    subtasks = [
        {"objective": "", "files": ["a.py"]},
        {"objective": "inspect", "files": ["outside.py"]},
        {"objective": "inspect", "files": ["a.py"]},
        {"objective": "inspect", "files": ["a.py"]},
    ]
    valid, errors = validate_subtasks(_parent(), subtasks, 8)
    assert len(valid) == 1
    assert any("empty objective" in e for e in errors)
    assert any("expands parent" in e for e in errors)
    assert any("duplicates" in e for e in errors)


def test_parse_bad_subtasks_is_explicit_error():
    assert parse_subtasks("no block") == ([], 1)
    assert parse_subtasks(SUBTASKS_BEGIN + "not json" + SUBTASKS_END) == ([], 1)


def test_dotfile_scope_is_not_rewritten():
    parent = _parent()
    parent = TaskEnvelope(
        parent.task_id, None, parent.campaign_id, parent.role, parent.goal_text, parent.cwd,
        metadata={**parent.metadata, "scope": [".env"]},
    )
    valid, errors = validate_subtasks(parent, [{"objective": "inspect config", "files": [".env"]}])
    assert errors == []
    assert valid[0]["files"] == [".env"]
