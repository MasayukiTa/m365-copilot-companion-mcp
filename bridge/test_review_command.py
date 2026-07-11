"""Unit tests for bridge/review_command.py -- pure parsing/formatting, no I/O/subprocess."""
from __future__ import annotations

from bridge.review_command import (
    build_review_argv,
    format_review_summary,
    parse_review_command,
    parse_run_output,
)


# ---------------------------------------------------------------------------
# parse_review_command
# ---------------------------------------------------------------------------

def test_parse_review_bare():
    assert parse_review_command("/review") == {
        "kind": "review", "mode": "all", "target_path": None,
    }


def test_parse_review_no_slash():
    assert parse_review_command("review") == {
        "kind": "review", "mode": "all", "target_path": None,
    }


def test_parse_review_diff():
    assert parse_review_command("/review diff") == {
        "kind": "review", "mode": "diff", "target_path": None,
    }


def test_parse_review_diff_case_insensitive():
    assert parse_review_command("/review DIFF") == {
        "kind": "review", "mode": "diff", "target_path": None,
    }


def test_parse_review_diff_ignores_trailing_text():
    assert parse_review_command("/review diff extra stuff") == {
        "kind": "review", "mode": "diff", "target_path": None,
    }


def test_parse_review_path():
    assert parse_review_command("/review src/foo") == {
        "kind": "review", "mode": "all", "target_path": "src/foo",
    }


def test_parse_review_path_with_spaces_preserved():
    assert parse_review_command("/review src/foo bar") == {
        "kind": "review", "mode": "all", "target_path": "src/foo bar",
    }


def test_parse_security_review_hyphen():
    assert parse_review_command("/security-review") == {
        "kind": "security", "mode": "all", "target_path": None,
    }


def test_parse_security_review_no_hyphen():
    assert parse_review_command("/securityreview") == {
        "kind": "security", "mode": "all", "target_path": None,
    }


def test_parse_security_review_diff():
    assert parse_review_command("/security-review diff") == {
        "kind": "security", "mode": "diff", "target_path": None,
    }


def test_parse_security_review_path():
    assert parse_review_command("/security-review src/bar") == {
        "kind": "security", "mode": "all", "target_path": "src/bar",
    }


def test_parse_review_empty_string():
    assert parse_review_command("") == {
        "kind": "review", "mode": "all", "target_path": None,
    }


def test_parse_review_whitespace_only():
    assert parse_review_command("   ") == {
        "kind": "review", "mode": "all", "target_path": None,
    }


def test_parse_review_none_input_never_raises():
    assert parse_review_command(None) == {
        "kind": "review", "mode": "all", "target_path": None,
    }


def test_parse_review_junk_leading_token_falls_back_to_review():
    assert parse_review_command("/nonsense diff") == {
        "kind": "review", "mode": "diff", "target_path": None,
    }


def test_parse_review_case_insensitive_leading_token():
    assert parse_review_command("/REVIEW src/foo") == {
        "kind": "review", "mode": "all", "target_path": "src/foo",
    }


# ---------------------------------------------------------------------------
# build_review_argv
# ---------------------------------------------------------------------------

def test_build_argv_no_target():
    parsed = {"kind": "review", "mode": "all", "target_path": None}
    argv = build_review_argv(parsed, r"C:\repo", r"C:\repo\.venv\Scripts\python.exe")
    assert argv[0] == r"C:\repo\.venv\Scripts\python.exe"
    assert argv[1].endswith("review_run.py")
    assert "bench" in argv[1]
    assert argv[2:6] == ["--kind", "review", "--mode", "all"]
    assert "--target-path" not in argv


def test_build_argv_with_target():
    parsed = {"kind": "security", "mode": "all", "target_path": "src/foo"}
    argv = build_review_argv(parsed, r"C:\repo", r"C:\repo\.venv\Scripts\python.exe")
    assert argv[2:6] == ["--kind", "security", "--mode", "all"]
    assert argv[6:8] == ["--target-path", "src/foo"]


def test_build_argv_diff_mode():
    parsed = {"kind": "review", "mode": "diff", "target_path": None}
    argv = build_review_argv(parsed, r"C:\repo", r"C:\repo\.venv\Scripts\python.exe")
    assert argv[2:6] == ["--kind", "review", "--mode", "diff"]


def test_build_argv_defaults_on_missing_keys():
    argv = build_review_argv({}, r"C:\repo", r"C:\repo\.venv\Scripts\python.exe")
    assert argv[2:6] == ["--kind", "review", "--mode", "all"]


# ---------------------------------------------------------------------------
# parse_run_output
# ---------------------------------------------------------------------------

def test_parse_run_output_both_lines():
    stdout = (
        "launching 3 review goal(s) on the free M365 fleet...\n"
        "fleet: -m relay.fleet_runner --goals-file x\n"
        "report: C:\\repo\\.fleet\\review\\review_report_20260101_000000.md\n"
        "summary: high=2 medium=1 low=3 parse_errors=0\n"
    )
    info = parse_run_output(stdout)
    assert info["report_md"] == "C:\\repo\\.fleet\\review\\review_report_20260101_000000.md"
    assert info["counts"] == {"high": 2, "medium": 1, "low": 3, "parse_errors": 0}


def test_parse_run_output_missing_lines():
    info = parse_run_output("some unrelated output\nno matches here\n")
    assert info == {"report_md": None, "counts": None}


def test_parse_run_output_empty_string():
    assert parse_run_output("") == {"report_md": None, "counts": None}


def test_parse_run_output_none_input_never_raises():
    assert parse_run_output(None) == {"report_md": None, "counts": None}


def test_parse_run_output_report_only():
    info = parse_run_output("report: /tmp/report.md\n")
    assert info["report_md"] == "/tmp/report.md"
    assert info["counts"] is None


def test_parse_run_output_summary_only():
    info = parse_run_output("summary: high=0 medium=0 low=0 parse_errors=1\n")
    assert info["report_md"] is None
    assert info["counts"] == {"high": 0, "medium": 0, "low": 0, "parse_errors": 1}


def test_parse_run_output_malformed_summary_tolerated():
    info = parse_run_output("summary: high=oops medium=1\n")
    assert info["counts"] == {"medium": 1}


# ---------------------------------------------------------------------------
# format_review_summary
# ---------------------------------------------------------------------------

def test_format_summary_with_high_medium_low():
    counts = {"high": 2, "medium": 1, "low": 3, "parse_errors": 0}
    agg_json = {
        "by_severity": {
            "high": [
                {"file": "a.py", "line": 10, "title": "SQL injection risk"},
                {"file": "b.py", "line": 20, "title": "Unsafe eval"},
            ],
            "medium": [
                {"file": "c.py", "line": 5, "title": "Missing null check"},
            ],
            "low": [
                {"file": "d.py", "line": 1, "title": "Style nit"},
            ],
        }
    }
    out = format_review_summary("review", counts, agg_json, "C:\\repo\\report.md")
    assert out.startswith("レビュー完了 (review)")
    assert "high=2 medium=1 low=3 parse_errors=0" in out
    assert "- [high] a.py:10 — SQL injection risk" in out
    assert "- [high] b.py:20 — Unsafe eval" in out
    assert "- [medium] c.py:5 — Missing null check" in out
    # low severity is never inlined
    assert "Style nit" not in out
    assert "詳細レポート: C:\\repo\\report.md (.json も同ディレクトリ)" in out


def test_format_summary_security_label():
    out = format_review_summary("security", {"high": 0, "medium": 0, "low": 0,
                                               "parse_errors": 0}, None, None)
    assert out.startswith("レビュー完了 (security)")


def test_format_summary_agg_json_none_fallback():
    counts = {"high": 1, "medium": 0, "low": 0, "parse_errors": 0}
    out = format_review_summary("review", counts, None, "C:\\repo\\report.md")
    assert "high=1 medium=0 low=0 parse_errors=0" in out
    assert "詳細レポート: C:\\repo\\report.md" in out
    assert "[high]" not in out  # no findings available to inline without agg_json


def test_format_summary_missing_counts_keys_defaults_to_zero():
    out = format_review_summary("review", {}, None, None)
    assert "high=0 medium=0 low=0 parse_errors=0" in out


def test_format_summary_no_report_path_skips_pointer_line():
    out = format_review_summary("review", {"high": 0, "medium": 0, "low": 0,
                                            "parse_errors": 0}, None, None)
    assert "詳細レポート" not in out


def test_format_summary_max_high_limit():
    agg_json = {
        "by_severity": {
            "high": [{"file": "f%d.py" % i, "line": i, "title": "t%d" % i} for i in range(5)],
            "medium": [],
            "low": [],
        }
    }
    out = format_review_summary("review", {"high": 5}, agg_json, None, max_high=2)
    assert out.count("- [high]") == 2


def test_format_summary_missing_finding_keys_tolerated():
    agg_json = {"by_severity": {"high": [{}], "medium": [], "low": []}}
    out = format_review_summary("review", {"high": 1}, agg_json, None)
    assert "- [high] ?:? — " in out


def test_format_summary_never_raises_on_garbage_agg_json():
    out = format_review_summary("review", {"high": 1}, {"by_severity": "not-a-dict"}, None)
    assert out.startswith("レビュー完了 (review)")
