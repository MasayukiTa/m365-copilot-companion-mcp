"""Hermetic unit tests for bench/review_run.py.

No fleet, no browser: run_fleet() is monkeypatched out in every test that would otherwise
reach the fleet subprocess. --dry-run tests exercise the REAL (non-monkeypatched) planning
code path and additionally install a guard that fails loudly if run_fleet is ever called,
proving --dry-run truly stops before launch.

  .venv\\Scripts\\python.exe -m pytest bench/test_review_run.py -q
"""
import json
import os
import subprocess
import sys

import pytest

import bench.review_run as review_run
from bench.review_run import fleet_cmd, main, merge_verdicts, plan_goals, verify_goals_from_findings


def _run(cmd, cwd):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, "cmd %r failed: %s\n%s" % (cmd, r.stdout, r.stderr)
    return r


def _init_repo(root):
    _run(["git", "init"], root)
    _run(["git", "config", "user.email", "fake@example.invalid"], root)
    _run(["git", "config", "user.name", "Fake Tester"], root)


def _write(root, rel, content="hello\n"):
    full = os.path.join(root, rel)
    if os.path.dirname(rel):
        os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return full


def _commit_all(root, msg="init"):
    _run(["git", "add", "-A"], root)
    _run(["git", "commit", "-m", msg], root)


@pytest.fixture
def repo(tmp_path):
    root = str(tmp_path / "fakerepo")
    os.makedirs(root, exist_ok=True)
    _init_repo(root)
    _write(root, "a.py", "print(1)\n")
    _write(root, "sub/b.py", "print(2)\n")
    _commit_all(root)
    return root


def _no_fleet_guard(monkeypatch):
    """Install a run_fleet stub that fails the test loudly if it's ever actually called --
    used by tests whose whole point is that run_fleet must NOT run."""
    def _boom(*a, **kw):
        raise AssertionError("run_fleet must not be called in this test")
    monkeypatch.setattr(review_run, "run_fleet", _boom)


# --- --dry-run: plan only, never touches run_fleet ------------------------------------------

def test_dry_run_builds_goals_and_never_calls_fleet(repo, monkeypatch, capsys):
    monkeypatch.setattr(review_run, "REPO", repo)
    _no_fleet_guard(monkeypatch)

    rc = main(["--kind", "review", "--dry-run", "--group-size", "1"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "goals: 2" in out  # a.py, sub/b.py each in their own goal (group-size 1)

    goals_dir = os.path.join(repo, ".fleet", "review")
    goal_files = [f for f in os.listdir(goals_dir) if f.startswith("goals_")]
    assert len(goal_files) == 1
    with open(os.path.join(goals_dir, goal_files[0]), encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert len(lines) == 2

    # no report should have been written -- dry-run stops before aggregation
    reports = [f for f in os.listdir(goals_dir) if f.startswith("review_report_")]
    assert reports == []


def test_dry_run_prints_exact_fleet_cmd(repo, monkeypatch, capsys):
    monkeypatch.setattr(review_run, "REPO", repo)
    _no_fleet_guard(monkeypatch)

    rc = main(["--kind", "security", "--dry-run", "--max-concurrent", "7", "--effort", "min"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "'--max-concurrent', '7'" in out
    assert "'--effort', 'min'" in out
    assert "relay.fleet_runner" in out


# --- fleet_cmd(): the verified launch contract, byte for byte ------------------------------

def test_fleet_cmd_shape():
    cmd = fleet_cmd("C:/x/goals.jsonl", 4, "auto")
    assert cmd == [
        review_run.VENVPY, "-m", "relay.fleet_runner",
        "--goals-file", "C:/x/goals.jsonl",
        "--max-concurrent", "4",
        "--max-turns", "40",
        "--disk-floor-gb", "0",
        "--effort", "auto",
    ]


# --- verify_goals_from_findings: pure, no fleet ---------------------------------------------

def test_verify_goals_from_findings_contains_file_and_title_and_verdict_instruction():
    findings = [
        {"file": "pkg/a.py", "line": 10, "severity": "high", "title": "SQL injection",
         "detail": "unescaped input"},
        {"file": "pkg/b.py", "line": None, "severity": "low", "title": "unused import",
         "detail": ""},
    ]
    goals = verify_goals_from_findings(findings, "C:/fakerepo", batch_size=10)
    assert len(goals) == 1
    text = goals[0]["text"]
    assert goals[0]["cwd"] == "C:/fakerepo"
    for f in findings:
        assert f["file"] in text
        assert f["title"] in text
    assert review_run.FINDINGS_BEGIN in text
    assert review_run.FINDINGS_END in text
    assert "verdict" in text
    assert "DONE" in text


def test_verify_goals_from_findings_batches():
    findings = [{"file": "f%d.py" % i, "line": i, "severity": "low", "title": "t%d" % i,
                 "detail": ""} for i in range(23)]
    goals = verify_goals_from_findings(findings, "C:/fakerepo", batch_size=10)
    assert len(goals) == 3  # 10 + 10 + 3
    all_text = " ".join(g["text"] for g in goals)
    for f in findings:
        assert f["file"] in all_text


# --- merge_verdicts: pure --------------------------------------------------------------------

def test_merge_verdicts_matches_by_file_line_title():
    agg = {"findings": [
        {"file": "a.py", "line": 10, "title": "X", "severity": "high"},
        {"file": "b.py", "line": 5, "title": "Y", "severity": "low"},
    ]}
    verdicts = [
        {"file": "a.py", "line": 10, "title": "X", "verdict": "confirmed", "reason": "yep"},
    ]
    merge_verdicts(agg, verdicts)
    assert agg["findings"][0]["verify_verdict"] == "confirmed"
    assert agg["findings"][0]["verify_reason"] == "yep"
    assert agg["findings"][1]["verify_verdict"] is None


# --- non-dry-run: run_fleet monkeypatched to a stub that writes a fixture status.json -------

def _fake_run_fleet_factory(repo_root, status_payload):
    """Returns a run_fleet stub that writes status_payload as .fleet/status.json under
    repo_root, mimicking what a real relay.fleet_runner run leaves behind."""
    def _fake_run_fleet(goals_path, max_concurrent, effort):
        fleet_dir = os.path.join(repo_root, ".fleet")
        os.makedirs(fleet_dir, exist_ok=True)
        with open(os.path.join(fleet_dir, "status.json"), "w", encoding="utf-8") as f:
            json.dump(status_payload, f)
        return 0
    return _fake_run_fleet


def test_non_dry_run_produces_report_matching_fixture(repo, monkeypatch, capsys):
    monkeypatch.setattr(review_run, "REPO", repo)
    monkeypatch.setattr(review_run, "VENVPY", sys.executable)  # must exist; never invoked

    findings_json = json.dumps([
        {"file": "a.py", "line": 1, "severity": "high", "title": "bad", "detail": "d"},
        {"file": "sub/b.py", "line": 2, "severity": "low", "title": "minor", "detail": "d2"},
    ], ensure_ascii=False)
    worker_text = (
        "some prose\n" +
        review_run.FINDINGS_BEGIN + "\n" + findings_json + "\n" + review_run.FINDINGS_END +
        "\nDONE"
    )
    status_payload = {"running": False, "workers": [
        {"name": "w1", "goal": "review a.py, sub/b.py", "outcome": "done",
         "reason": "", "verified": True, "transcript": "", "display_result": worker_text},
    ]}
    monkeypatch.setattr(review_run, "run_fleet",
                         _fake_run_fleet_factory(repo, status_payload))

    rc = main(["--kind", "review", "--group-size", "10"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "high=1 medium=0 low=1 parse_errors=0" in out

    out_dir = os.path.join(repo, ".fleet", "review")
    md_files = [f for f in os.listdir(out_dir)
                if f.startswith("review_report_") and f.endswith(".md")]
    json_files = [f for f in os.listdir(out_dir)
                  if f.startswith("review_report_") and f.endswith(".json")]
    assert len(md_files) == 1 and len(json_files) == 1

    with open(os.path.join(out_dir, json_files[0]), encoding="utf-8") as f:
        report = json.load(f)
    assert len(report["findings"]) == 2
    assert report["parse_errors"] == 0
    assert len(report["by_severity"]["high"]) == 1
    assert len(report["by_severity"]["low"]) == 1


# --- degradation paths: must not crash -------------------------------------------------------

def test_missing_venv_reports_error_without_crashing(repo, monkeypatch, capsys):
    monkeypatch.setattr(review_run, "REPO", repo)
    monkeypatch.setattr(review_run, "VENVPY", os.path.join(repo, "nope", "python.exe"))
    _no_fleet_guard(monkeypatch)

    rc = main(["--kind", "review"])
    assert rc == 1
    out = capsys.readouterr().out
    assert ".venv python not found" in out


def test_missing_status_json_after_run_reports_error_without_crashing(repo, monkeypatch, capsys):
    monkeypatch.setattr(review_run, "REPO", repo)
    monkeypatch.setattr(review_run, "VENVPY", sys.executable)

    def _fake_run_fleet(goals_path, max_concurrent, effort):
        return 1  # simulate a fleet that failed to start; no status.json written

    monkeypatch.setattr(review_run, "run_fleet", _fake_run_fleet)

    rc = main(["--kind", "review"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "status.json" in out
    assert "not found" in out

    out_dir = os.path.join(repo, ".fleet", "review")
    reports = [f for f in os.listdir(out_dir) if f.startswith("review_report_")]
    assert reports == []


def test_no_files_matched_returns_zero_without_fleet(tmp_path, monkeypatch, capsys):
    root = str(tmp_path / "emptyrepo")
    os.makedirs(root, exist_ok=True)
    _init_repo(root)  # no files added/committed at all -> enumerate_files returns []

    monkeypatch.setattr(review_run, "REPO", root)
    monkeypatch.setattr(review_run, "VENVPY", sys.executable)
    _no_fleet_guard(monkeypatch)

    rc = main(["--kind", "review"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "nothing to review" in out


# --- _stamp() is monkeypatchable for deterministic filenames --------------------------------

def test_stamp_monkeypatchable(repo, monkeypatch):
    monkeypatch.setattr(review_run, "REPO", repo)
    monkeypatch.setattr(review_run, "_stamp", lambda: "FIXEDSTAMP")
    _no_fleet_guard(monkeypatch)

    rc = main(["--kind", "review", "--dry-run"])
    assert rc == 0
    goals_dir = os.path.join(repo, ".fleet", "review")
    assert os.path.isfile(os.path.join(goals_dir, "goals_FIXEDSTAMP.jsonl"))


# --- plan_goals(): mode=diff defaults to one goal for the whole changeset -------------------

def test_plan_goals_diff_mode_defaults_to_one_group(repo):
    _write(repo, "a.py", "one\ntwo\n")  # modify tracked file, unstaged
    _write(repo, "new.py", "new\n")     # untracked -- diff --name-only won't see this one

    files, groups, goals = plan_goals("review", "diff", repo)
    assert files == ["a.py"]
    assert groups == [["a.py"]]
    assert len(goals) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
