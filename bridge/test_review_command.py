"""Unit tests for bridge/review_command.py -- pure parsing/formatting, no I/O/subprocess."""
from __future__ import annotations

from bridge.review_command import (
    build_review_argv,
    build_review_fix_argv,
    format_fix_summary,
    format_review_summary,
    parse_fix_run_output,
    parse_review_command,
    parse_review_fix_command,
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
        "launching 3 review goal(s)...\n"
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


# ---------------------------------------------------------------------------
# parse_review_fix_command
# ---------------------------------------------------------------------------

def test_parse_review_fix_bare():
    assert parse_review_fix_command("/review-fix") == {
        "confirm": False, "min_severity": "medium", "verified_only": False,
    }


def test_parse_review_fix_confirm():
    assert parse_review_fix_command("/review-fix confirm") == {
        "confirm": True, "min_severity": "medium", "verified_only": False,
    }


def test_parse_review_fix_confirm_case_insensitive():
    assert parse_review_fix_command("/review-fix CONFIRM") == {
        "confirm": True, "min_severity": "medium", "verified_only": False,
    }


def test_parse_review_fix_high():
    assert parse_review_fix_command("/review-fix high") == {
        "confirm": False, "min_severity": "high", "verified_only": False,
    }


def test_parse_review_fix_verified():
    assert parse_review_fix_command("/review-fix verified") == {
        "confirm": False, "min_severity": "medium", "verified_only": True,
    }


def test_parse_review_fix_confirm_high_verified_combo():
    assert parse_review_fix_command("/review-fix confirm high verified") == {
        "confirm": True, "min_severity": "high", "verified_only": True,
    }


def test_parse_review_fix_no_slash():
    assert parse_review_fix_command("review-fix confirm") == {
        "confirm": True, "min_severity": "medium", "verified_only": False,
    }


def test_parse_review_fix_junk_token_ignored():
    assert parse_review_fix_command("/review-fix nonsense") == {
        "confirm": False, "min_severity": "medium", "verified_only": False,
    }


def test_parse_review_fix_confirm_only_as_first_token():
    # "confirm" appearing AFTER other junk (not as the first remaining token) does not arm it.
    assert parse_review_fix_command("/review-fix high confirm") == {
        "confirm": False, "min_severity": "high", "verified_only": False,
    }


def test_parse_review_fix_empty_string():
    assert parse_review_fix_command("") == {
        "confirm": False, "min_severity": "medium", "verified_only": False,
    }


def test_parse_review_fix_whitespace_only():
    assert parse_review_fix_command("   ") == {
        "confirm": False, "min_severity": "medium", "verified_only": False,
    }


def test_parse_review_fix_none_input_never_raises():
    assert parse_review_fix_command(None) == {
        "confirm": False, "min_severity": "medium", "verified_only": False,
    }


# ---------------------------------------------------------------------------
# build_review_fix_argv
# ---------------------------------------------------------------------------

def test_build_fix_argv_dry_run_default_severity():
    parsed = {"confirm": False, "min_severity": "medium", "verified_only": False}
    argv = build_review_fix_argv(parsed, r"C:\repo", r"C:\repo\.venv\Scripts\python.exe",
                                  dry_run=True)
    assert argv[0] == r"C:\repo\.venv\Scripts\python.exe"
    assert argv[1].endswith("review_fix.py")
    assert "bench" in argv[1]
    assert "--dry-run" in argv
    assert argv[argv.index("--min-severity") + 1] == "medium"
    assert "--verified-only" not in argv


def test_build_fix_argv_no_dry_run_omits_flag():
    parsed = {"confirm": True, "min_severity": "medium", "verified_only": False}
    argv = build_review_fix_argv(parsed, r"C:\repo", r"C:\repo\.venv\Scripts\python.exe",
                                  dry_run=False)
    assert "--dry-run" not in argv


def test_build_fix_argv_high_severity():
    parsed = {"confirm": False, "min_severity": "high", "verified_only": False}
    argv = build_review_fix_argv(parsed, r"C:\repo", r"C:\repo\.venv\Scripts\python.exe",
                                  dry_run=True)
    assert argv[argv.index("--min-severity") + 1] == "high"


def test_build_fix_argv_verified_only():
    parsed = {"confirm": False, "min_severity": "medium", "verified_only": True}
    argv = build_review_fix_argv(parsed, r"C:\repo", r"C:\repo\.venv\Scripts\python.exe",
                                  dry_run=True)
    assert "--verified-only" in argv


def test_build_fix_argv_defaults_on_missing_keys():
    argv = build_review_fix_argv({}, r"C:\repo", r"C:\repo\.venv\Scripts\python.exe",
                                  dry_run=True)
    assert argv[argv.index("--min-severity") + 1] == "medium"
    assert "--verified-only" not in argv


# ---------------------------------------------------------------------------
# parse_fix_run_output
# ---------------------------------------------------------------------------

def test_parse_fix_run_output_full():
    stdout = (
        "backed up 2 file(s) to C:\\repo\\.fleet\\review_fix\\backup_20260101_000000\n"
        "launching 1 fix goal(s)...\n"
        "fix report: C:\\repo\\.fleet\\review_fix\\fix_report_20260101_000000.md\n"
        "applied=3 skipped=1 test_gate=PASSED\n"
        "backup: C:\\repo\\.fleet\\review_fix\\backup_20260101_000000\n"
        "undo: C:\\repo\\.fleet\\review_fix\\undo_20260101_000000.bat "
        "(or `C:\\repo\\.venv\\Scripts\\python.exe C:\\repo\\bench\\review_fix.py "
        "--undo 20260101_000000`)\n"
        "branch: review-fix-20260101_000000\n"
    )
    info = parse_fix_run_output(stdout)
    assert info["fix_report_md"] == \
        "C:\\repo\\.fleet\\review_fix\\fix_report_20260101_000000.md"
    assert info["applied"] == 3
    assert info["skipped"] == 1
    assert info["test_gate"] == "PASSED"
    assert info["backup_dir"] == "C:\\repo\\.fleet\\review_fix\\backup_20260101_000000"
    assert info["undo_line"].startswith("C:\\repo\\.fleet\\review_fix\\undo_20260101_000000.bat")
    assert info["branch"] == "review-fix-20260101_000000"
    assert info["test_gate_failed"] is False


def test_parse_fix_run_output_test_gate_failed():
    stdout = (
        "applied=1 skipped=0 test_gate=FAILED\n"
        "TEST GATE FAILED -- NOT reverted. Inspect manually; undo available above.\n"
    )
    info = parse_fix_run_output(stdout)
    assert info["test_gate"] == "FAILED"
    assert info["test_gate_failed"] is True


def test_parse_fix_run_output_empty_string():
    info = parse_fix_run_output("")
    assert info["fix_report_md"] is None
    assert info["applied"] is None
    assert info["test_gate_failed"] is False


def test_parse_fix_run_output_none_input_never_raises():
    info = parse_fix_run_output(None)
    assert info["applied"] is None


def test_parse_fix_run_output_no_branch_line_stays_none():
    info = parse_fix_run_output("applied=0 skipped=0 test_gate=PASSED\n")
    assert info["branch"] is None


# ---------------------------------------------------------------------------
# format_fix_summary
# ---------------------------------------------------------------------------

def test_format_fix_summary_full():
    info = {
        "fix_report_md": "C:\\repo\\fix_report.md",
        "applied": 3, "skipped": 1, "test_gate": "PASSED",
        "backup_dir": "C:\\repo\\backup_x", "undo_line": "undo_x.bat (or `cmd`)",
        "branch": "review-fix-x", "test_gate_failed": False,
    }
    out = format_fix_summary(info)
    assert out.startswith("修正完了")
    assert "applied=3 skipped=1 test_gate=PASSED" in out
    assert "バックアップ: C:\\repo\\backup_x" in out
    assert "元に戻すには: undo_x.bat (or `cmd`)" in out
    assert "git ブランチ: review-fix-x" in out
    assert "詳細レポート: C:\\repo\\fix_report.md" in out
    assert "テストゲート失敗" not in out


def test_format_fix_summary_test_gate_failed_warns():
    info = {"applied": 1, "skipped": 0, "test_gate": "FAILED", "test_gate_failed": True}
    out = format_fix_summary(info)
    assert "テストゲート失敗" in out


def test_format_fix_summary_missing_fields_defaults():
    out = format_fix_summary({})
    assert "applied=0 skipped=0 test_gate=unknown" in out
    assert "バックアップ" not in out
    assert "詳細レポート" not in out


def test_format_fix_summary_none_input_never_raises():
    out = format_fix_summary(None)
    assert out.startswith("修正完了")
