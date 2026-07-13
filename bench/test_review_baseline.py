"""Hermetic unit tests for bench/review_baseline.py.

tmp_path only -- no network, no fleet, no real repo.

  .venv\\Scripts\\python.exe -m pytest bench/test_review_baseline.py -q
"""
import json
import os

import pytest

from bench.review_baseline import (
    dedupe_key,
    diff_against_baseline,
    load_baseline,
    save_baseline,
    should_gate,
)


# --- dedupe_key --------------------------------------------------------------------------

def test_dedupe_key_shape():
    f = {"file": "a/b.py", "line": 10, "title": "  Some Title  "}
    assert dedupe_key(f) == (os.path.normpath("a/b.py"), 10, "some title")


def test_dedupe_key_normalizes_path_separators():
    f1 = {"file": "a/b.py", "line": 1, "title": "X"}
    f2 = {"file": "a\\b.py", "line": 1, "title": "X"}
    assert dedupe_key(f1) == dedupe_key(f2)


def test_dedupe_key_case_and_whitespace_insensitive_title():
    f1 = {"file": "a.py", "line": 1, "title": "Bad Thing"}
    f2 = {"file": "a.py", "line": 1, "title": "  bad thing  "}
    assert dedupe_key(f1) == dedupe_key(f2)


def test_dedupe_key_missing_fields_never_raises():
    assert dedupe_key({}) == (os.path.normpath(""), None, "")
    assert dedupe_key({"file": "a.py"}) == (os.path.normpath("a.py"), None, "")


def test_dedupe_key_non_dict_never_raises():
    assert dedupe_key(None) == (os.path.normpath(""), None, "")
    assert dedupe_key("garbage") == (os.path.normpath(""), None, "")
    assert dedupe_key(42) == (os.path.normpath(""), None, "")


# --- load_baseline -----------------------------------------------------------------------

def test_load_baseline_missing_file_returns_empty_not_an_error(tmp_path):
    baseline = load_baseline(str(tmp_path / "nope.json"))
    assert baseline == {"version": 1, "accepted": []}


def test_load_baseline_well_formed_file(tmp_path):
    path = str(tmp_path / "baseline.json")
    payload = {"version": 1, "kind": "review", "generated_at": 123.0, "accepted": [
        {"file": "a.py", "line": 1, "title": "T", "severity": "high",
         "key": [os.path.normpath("a.py"), 1, "t"]},
    ]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    baseline = load_baseline(path)
    assert baseline == payload


def test_load_baseline_corrupt_json_raises_valueerror(tmp_path):
    path = str(tmp_path / "corrupt.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not valid json,,,")
    with pytest.raises(ValueError):
        load_baseline(path)


def test_load_baseline_not_an_object_raises_valueerror(tmp_path):
    path = str(tmp_path / "array.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump([1, 2, 3], f)
    with pytest.raises(ValueError):
        load_baseline(path)


def test_load_baseline_missing_accepted_key_raises_valueerror(tmp_path):
    path = str(tmp_path / "no_accepted.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1}, f)
    with pytest.raises(ValueError):
        load_baseline(path)


def test_load_baseline_path_is_a_directory_raises_valueerror(tmp_path):
    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(ValueError):
        load_baseline(str(d))


def test_load_baseline_normalizes_run_report_shape_confirmed_only(tmp_path):
    """A review_report_<stamp>.json (has "findings"/"by_severity", not "accepted") must load
    just like a real baseline file -- selecting only verify_verdict=="confirmed" findings when
    at least one finding carries verify data."""
    path = str(tmp_path / "review_report_20260101_000000.json")
    report = {
        "generated_at": 1.0, "workers_total": 1, "parse_errors": 0,
        "findings": [
            {"file": "a.py", "line": 1, "title": "Confirmed one", "severity": "high",
             "verify_verdict": "confirmed"},
            {"file": "b.py", "line": 2, "title": "Refuted one", "severity": "low",
             "verify_verdict": "false_positive"},
            {"file": "c.py", "line": 3, "title": "Unclear one", "severity": "medium",
             "verify_verdict": "unclear"},
        ],
        "by_severity": {"high": [], "medium": [], "low": []},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f)

    baseline = load_baseline(path)
    assert baseline["version"] == 1
    assert len(baseline["accepted"]) == 1
    assert baseline["accepted"][0]["title"] == "Confirmed one"
    assert baseline["accepted"][0]["key"] == [os.path.normpath("a.py"), 1, "confirmed one"]


def test_load_baseline_normalizes_run_report_shape_no_verify_data_accepts_all(tmp_path):
    """A run report built with --no-refute has no verify_verdict on any finding -- in that
    case every reported finding is accepted (there is no verdict data to be selective about)."""
    path = str(tmp_path / "review_report_20260101_000001.json")
    report = {
        "generated_at": 1.0, "workers_total": 1, "parse_errors": 0,
        "findings": [
            {"file": "a.py", "line": 1, "title": "One", "severity": "high"},
            {"file": "b.py", "line": 2, "title": "Two", "severity": "low"},
        ],
        "by_severity": {"high": [], "medium": [], "low": []},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f)

    baseline = load_baseline(path)
    assert len(baseline["accepted"]) == 2
    assert {e["title"] for e in baseline["accepted"]} == {"One", "Two"}


def test_load_baseline_run_report_with_no_findings_at_all(tmp_path):
    path = str(tmp_path / "review_report_20260101_000002.json")
    report = {"generated_at": 1.0, "workers_total": 0, "parse_errors": 0, "findings": [],
              "by_severity": {"high": [], "medium": [], "low": []}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f)
    baseline = load_baseline(path)
    assert baseline == {"version": 1, "accepted": []}


def test_load_baseline_does_not_silently_produce_empty_on_corrupt_file(tmp_path):
    """The core contract: a bad --baseline path must be a loud failure, never a silent empty
    baseline that would hide real findings from a CI gate."""
    path = str(tmp_path / "corrupt2.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("not json at all")
    try:
        load_baseline(path)
        assert False, "expected ValueError"
    except ValueError:
        pass


# --- save_baseline -------------------------------------------------------------------------

def test_save_baseline_round_trips_through_load_baseline(tmp_path):
    path = str(tmp_path / "out" / "baseline.json")
    findings = [
        {"file": "a.py", "line": 1, "title": "Bug One", "severity": "high", "extra": "dropped"},
        {"file": "b.py", "line": 2, "title": "Bug Two", "severity": "low"},
    ]
    save_baseline(path, findings, kind="review", generated_at=42.0)

    assert os.path.isfile(path)
    baseline = load_baseline(path)
    assert baseline["version"] == 1
    assert baseline["kind"] == "review"
    assert baseline["generated_at"] == 42.0
    assert len(baseline["accepted"]) == 2

    entry0 = baseline["accepted"][0]
    assert entry0["file"] == "a.py"
    assert entry0["line"] == 1
    assert entry0["title"] == "Bug One"
    assert entry0["severity"] == "high"
    assert entry0["key"] == [os.path.normpath("a.py"), 1, "bug one"]
    assert "extra" not in entry0  # only the documented fields are extracted


def test_save_baseline_atomic_write_no_leftover_tmp_file(tmp_path):
    path = str(tmp_path / "baseline.json")
    save_baseline(path, [], kind="security", generated_at=1.0)
    assert os.path.isfile(path)
    assert not os.path.isfile(path + ".tmp")


def test_save_baseline_never_calls_wall_clock(tmp_path):
    """generated_at is caller-supplied -- save_baseline must write exactly what it was given,
    not something computed from time.time() internally."""
    path = str(tmp_path / "baseline.json")
    save_baseline(path, [], kind="review", generated_at="FIXED_STAMP")
    baseline = load_baseline(path)
    assert baseline["generated_at"] == "FIXED_STAMP"


def test_save_baseline_skips_non_dict_entries(tmp_path):
    path = str(tmp_path / "baseline.json")
    save_baseline(path, [{"file": "a.py", "line": 1, "title": "T", "severity": "low"},
                          "garbage", None, 42],
                  kind="review", generated_at=1.0)
    baseline = load_baseline(path)
    assert len(baseline["accepted"]) == 1


def test_save_baseline_empty_accepted_list(tmp_path):
    path = str(tmp_path / "baseline.json")
    save_baseline(path, [], kind="review", generated_at=1.0)
    baseline = load_baseline(path)
    assert baseline["accepted"] == []


# --- diff_against_baseline -----------------------------------------------------------------

def _baseline_with(*entries):
    return {"version": 1, "accepted": list(entries)}


def _entry(file, line, title, severity="low"):
    return {"file": file, "line": line, "title": title, "severity": severity,
            "key": [os.path.normpath(file), line, title.strip().lower()]}


def test_diff_new_finding_not_in_baseline():
    baseline = _baseline_with()
    findings = [{"file": "a.py", "line": 1, "title": "Fresh bug", "severity": "high"}]
    diff = diff_against_baseline(findings, baseline)
    assert diff["new"] == findings
    assert diff["regressed"] == []
    assert diff["unchanged"] == []
    assert diff["resolved"] == []


def test_diff_reconfirmed_baseline_finding_is_regressed():
    baseline = _baseline_with(_entry("a.py", 1, "Known bug", "high"))
    findings = [{"file": "a.py", "line": 1, "title": "Known bug", "severity": "high",
                 "verify_verdict": "confirmed"}]
    diff = diff_against_baseline(findings, baseline)
    assert diff["new"] == []
    assert diff["regressed"] == findings
    assert diff["unchanged"] == []
    assert diff["resolved"] == []


def test_diff_baseline_finding_without_verify_verdict_is_regressed_conservatively():
    """No refute data (verify_verdict missing/None) must NOT silently hide a regression."""
    baseline = _baseline_with(_entry("a.py", 1, "Known bug", "high"))
    findings = [{"file": "a.py", "line": 1, "title": "Known bug", "severity": "high"}]
    diff = diff_against_baseline(findings, baseline)
    assert diff["regressed"] == findings
    assert diff["new"] == []
    assert diff["unchanged"] == []


def test_diff_baseline_finding_not_reconfirmed_is_unchanged():
    baseline = _baseline_with(_entry("a.py", 1, "Known bug", "high"))
    findings = [{"file": "a.py", "line": 1, "title": "Known bug", "severity": "high",
                 "verify_verdict": "unclear"}]
    diff = diff_against_baseline(findings, baseline)
    assert diff["unchanged"] == findings
    assert diff["new"] == []
    assert diff["regressed"] == []

    findings2 = [{"file": "a.py", "line": 1, "title": "Known bug", "severity": "high",
                  "verify_verdict": "false_positive"}]
    diff2 = diff_against_baseline(findings2, baseline)
    assert diff2["unchanged"] == findings2


def test_diff_baseline_finding_absent_from_current_run_is_resolved():
    entry = _entry("a.py", 1, "Fixed bug", "medium")
    baseline = _baseline_with(entry)
    diff = diff_against_baseline([], baseline)
    assert diff["resolved"] == [entry]
    assert diff["new"] == []
    assert diff["regressed"] == []
    assert diff["unchanged"] == []


def test_diff_mixed_case_all_four_buckets():
    baseline = _baseline_with(
        _entry("regressed.py", 1, "Regressed", "high"),
        _entry("unchanged.py", 2, "Unchanged", "low"),
        _entry("resolved.py", 3, "Resolved", "medium"),
    )
    findings = [
        {"file": "regressed.py", "line": 1, "title": "Regressed", "severity": "high",
         "verify_verdict": "confirmed"},
        {"file": "unchanged.py", "line": 2, "title": "Unchanged", "severity": "low",
         "verify_verdict": "unclear"},
        {"file": "new.py", "line": 9, "title": "New one", "severity": "medium"},
    ]
    diff = diff_against_baseline(findings, baseline)
    assert len(diff["new"]) == 1 and diff["new"][0]["title"] == "New one"
    assert len(diff["regressed"]) == 1 and diff["regressed"][0]["title"] == "Regressed"
    assert len(diff["unchanged"]) == 1 and diff["unchanged"][0]["title"] == "Unchanged"
    assert len(diff["resolved"]) == 1 and diff["resolved"][0]["title"] == "Resolved"


def test_diff_defensive_against_non_dict_entries():
    baseline = {"accepted": [_entry("a.py", 1, "T"), "garbage", None]}
    findings = [{"file": "a.py", "line": 1, "title": "T", "severity": "low"}, "garbage", None]
    diff = diff_against_baseline(findings, baseline)
    assert diff  # never raises


def test_diff_falsy_baseline_treats_everything_as_new():
    findings = [{"file": "a.py", "line": 1, "title": "T", "severity": "low"}]
    diff = diff_against_baseline(findings, {})
    assert diff["new"] == findings
    diff2 = diff_against_baseline(findings, None)
    assert diff2["new"] == findings


# --- should_gate -----------------------------------------------------------------------------

def _diff(new=None, regressed=None):
    return {"new": new or [], "regressed": regressed or [], "resolved": [], "unchanged": []}


def test_should_gate_none_never_gates():
    diff = _diff(new=[{"file": "a.py", "line": 1, "title": "T", "severity": "high"}])
    should_fail, offending = should_gate(diff, None)
    assert should_fail is False
    assert offending == []


def test_should_gate_below_threshold_does_not_gate():
    diff = _diff(new=[{"file": "a.py", "line": 1, "title": "T", "severity": "low"}])
    should_fail, offending = should_gate(diff, "high")
    assert should_fail is False
    assert offending == []


def test_should_gate_at_threshold_gates():
    diff = _diff(new=[{"file": "a.py", "line": 1, "title": "T", "severity": "medium"}])
    should_fail, offending = should_gate(diff, "medium")
    assert should_fail is True
    assert len(offending) == 1


def test_should_gate_above_threshold_gates():
    diff = _diff(regressed=[{"file": "a.py", "line": 1, "title": "T", "severity": "high"}])
    should_fail, offending = should_gate(diff, "low")
    assert should_fail is True
    assert len(offending) == 1


def test_should_gate_severity_ordering_low_medium_high():
    high = {"file": "a.py", "line": 1, "title": "H", "severity": "high"}
    medium = {"file": "b.py", "line": 2, "title": "M", "severity": "medium"}
    low = {"file": "c.py", "line": 3, "title": "L", "severity": "low"}
    diff = _diff(new=[high, medium, low])

    should_fail, offending = should_gate(diff, "high")
    assert should_fail is True
    assert offending == [high]

    should_fail, offending = should_gate(diff, "medium")
    assert should_fail is True
    assert {f["title"] for f in offending} == {"H", "M"}

    should_fail, offending = should_gate(diff, "low")
    assert should_fail is True
    assert {f["title"] for f in offending} == {"H", "M", "L"}


def test_should_gate_only_considers_new_and_regressed_not_unchanged_or_resolved():
    diff = {
        "new": [],
        "regressed": [],
        "unchanged": [{"file": "a.py", "line": 1, "title": "U", "severity": "high"}],
        "resolved": [{"file": "b.py", "line": 2, "title": "R", "severity": "high"}],
    }
    should_fail, offending = should_gate(diff, "low")
    assert should_fail is False
    assert offending == []


def test_should_gate_defensive_against_malformed_entries():
    diff = _diff(new=[{"file": "a.py", "line": 1, "title": "T"}, "garbage", None])
    should_fail, offending = should_gate(diff, "low")
    # missing/unrecognized severity never counts toward the gate, and never raises
    assert should_fail is False


if __name__ == "__main__":
    import sys
    raise SystemExit(pytest.main([__file__, "-q"] + sys.argv[1:]))
