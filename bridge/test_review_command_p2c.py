import os

from bridge.review_command import build_review_argv, parse_review_command


def test_review_2_selects_resilience_without_changing_normal_command():
    assert parse_review_command("/review") == {
        "kind": "review", "mode": "all", "target_path": None,
    }
    assert parse_review_command("/review-2 diff") == {
        "kind": "review", "mode": "diff", "target_path": None, "resilience": True,
    }
    assert parse_review_command("/security-review-2 src") == {
        "kind": "security", "mode": "all", "target_path": "src", "resilience": True,
    }


def test_review_2_argv_enables_matching_profile():
    parsed = parse_review_command("/security-review-2")
    argv = build_review_argv(parsed, "C:/repo", "python.exe")
    assert argv[:2] == ["python.exe", os.path.join("C:/repo", "bench", "review_run.py")]
    assert argv[-2:] == ["--resilience-profile", "security"]
