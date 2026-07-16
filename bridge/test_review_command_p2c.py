import os

from bridge.review_command import build_review_argv, parse_review_command


def test_deep_review_selects_resilience_without_changing_normal_command():
    assert parse_review_command("/review") == {
        "kind": "review", "mode": "all", "target_path": None,
    }
    assert parse_review_command("/deep-review diff") == {
        "kind": "review", "mode": "diff", "target_path": None, "resilience": True,
    }
    assert parse_review_command("/deep-security-review src") == {
        "kind": "security", "mode": "all", "target_path": "src", "resilience": True,
    }


def test_deep_review_argv_enables_matching_profile():
    parsed = parse_review_command("/deep-security-review")
    argv = build_review_argv(parsed, "C:/repo", "python.exe")
    assert argv[:2] == ["python.exe", os.path.join("C:/repo", "bench", "review_run.py")]
    assert argv[-4:] == ["--resilience-profile", "security", "--p2c-level", "1"]


def test_deep_review_argv_propagates_full_validation_level():
    parsed = parse_review_command("/deep-security-review")
    argv = build_review_argv(parsed, "C:/repo", "python.exe", p2c_level=2)
    assert argv[-4:] == ["--resilience-profile", "security", "--p2c-level", "2"]


def test_legacy_p2c_names_remain_compatibility_aliases():
    assert parse_review_command("/review-2 diff")["resilience"] is True
    assert parse_review_command("/security-review-2 src")["kind"] == "security"
