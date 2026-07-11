"""Hermetic unit tests for bench/review_fix.py (+ the FIX_RUBRIC/build_fix_goal additions to
bench/review_build_goals.py that it depends on).

No fleet, no network: run_fix_fleet() and run_test_gate() are monkeypatched out in every test
that would otherwise reach a real subprocess. git_bonus_* tests use a real tmp `git init`
fixture (the one required impurity, matching bench/test_review_build_goals.py's own style).

  .venv\\Scripts\\python.exe -m pytest bench/test_review_fix.py -q
"""
import json
import os
import subprocess
import sys

import pytest

import bench.review_fix as review_fix
import bench.review_run as review_run
from bench.review_build_goals import FINDINGS_BEGIN, FINDINGS_END, build_fix_goal
from bench.review_fix import (
    backup_files,
    build_arg_parser,
    filter_findings,
    find_latest_report,
    git_bonus_precheck,
    group_findings_by_file,
    load_manifest,
    load_report,
    main,
    undo,
    write_manifest,
    write_undo_bat,
)


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
    # mirrors this actual repo's .gitignore (`.fleet/` is ignored -- runtime state) so that
    # review_fix's own goals/backup/report writes under .fleet/ don't make git_bonus_precheck
    # see a "dirty" tree just because it did its job.
    _write(root, ".gitignore", ".fleet/\n")
    _commit_all(root)
    return root


def _finding(file="a.py", line=1, severity="medium", title="issue", detail="d",
             verified=None, verify_verdict=None):
    f = {"file": file, "line": line, "severity": severity, "title": title, "detail": detail}
    if verified is not None:
        f["verified"] = verified
    if verify_verdict is not None:
        f["verify_verdict"] = verify_verdict
    return f


def _no_fleet_guard(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("run_fix_fleet must not be called in this test")
    monkeypatch.setattr(review_fix, "run_fix_fleet", _boom)


# --- filter_findings ------------------------------------------------------------------------

def test_filter_findings_severity_boundaries():
    findings = [
        _finding(title="lo", severity="low"),
        _finding(title="med", severity="medium"),
        _finding(title="hi", severity="high"),
    ]
    assert [f["title"] for f in filter_findings(findings, min_severity="low")] == ["lo", "med", "hi"]
    assert [f["title"] for f in filter_findings(findings, min_severity="medium")] == ["med", "hi"]
    assert [f["title"] for f in filter_findings(findings, min_severity="high")] == ["hi"]


def test_filter_findings_unknown_severity_treated_as_low():
    findings = [_finding(title="weird", severity="???")]
    assert filter_findings(findings, min_severity="low") == findings
    assert filter_findings(findings, min_severity="medium") == []


def test_filter_findings_drops_false_positive_regardless_of_severity():
    findings = [
        _finding(title="fp-high", severity="high", verify_verdict="false_positive"),
        _finding(title="confirmed-high", severity="high", verify_verdict="confirmed"),
    ]
    kept = filter_findings(findings, min_severity="low")
    assert [f["title"] for f in kept] == ["confirmed-high"]


def test_filter_findings_verified_only_keeps_true_only(capsys):
    findings = [
        _finding(title="v-true", verified=True),
        _finding(title="v-false", verified=False),
        _finding(title="v-none", verified=None),
    ]
    kept = filter_findings(findings, min_severity="low", verified_only=True)
    assert [f["title"] for f in kept] == ["v-true"]
    # no warning should print -- a "verified" field IS present on at least one finding
    assert "verified data absent" not in capsys.readouterr().out


def test_filter_findings_verified_only_warns_when_field_absent(capsys):
    findings = [_finding(title="no-verified-field")]
    assert "verified" not in findings[0]
    kept = filter_findings(findings, min_severity="low", verified_only=True)
    out = capsys.readouterr().out
    assert "verified data absent" in out
    # NOT silently emptied: the severity-filtered findings are still returned
    assert [f["title"] for f in kept] == ["no-verified-field"]


# --- group_findings_by_file -------------------------------------------------------------------

def test_group_findings_by_file_keeps_file_together():
    findings = [
        _finding(file="a.py", line=1, title="a1"),
        _finding(file="b.py", line=1, title="b1"),
        _finding(file="a.py", line=2, title="a2"),
    ]
    groups = group_findings_by_file(findings, max_files_per_goal=5)
    assert len(groups) == 1
    files_seen = {f["file"] for f in groups[0]}
    assert files_seen == {"a.py", "b.py"}
    a_titles = [f["title"] for f in groups[0] if f["file"] == "a.py"]
    assert a_titles == ["a1", "a2"]


def test_group_findings_by_file_chunks_by_max_files():
    findings = [_finding(file="f%d.py" % i, title="t%d" % i) for i in range(7)]
    groups = group_findings_by_file(findings, max_files_per_goal=3)
    assert len(groups) == 3  # 3 + 3 + 1 files
    all_titles = [f["title"] for g in groups for f in g]
    assert sorted(all_titles) == sorted(f["title"] for f in findings)
    file_counts = [len({f["file"] for f in g}) for g in groups]
    assert file_counts == [3, 3, 1]


def test_group_findings_by_file_never_splits_a_files_findings_across_groups():
    findings = [_finding(file="busy.py", line=i, title="t%d" % i) for i in range(4)]
    findings += [_finding(file="other.py", line=1, title="o1")]
    groups = group_findings_by_file(findings, max_files_per_goal=1)
    busy_group = [g for g in groups if any(f["file"] == "busy.py" for f in g)][0]
    assert len([f for f in busy_group if f["file"] == "busy.py"]) == 4
    assert all(f["file"] == "busy.py" for f in busy_group)  # not mixed with other.py


# --- build_fix_goal (bench.review_build_goals) -------------------------------------------------

def test_build_fix_goal_shape():
    group = [_finding(file="pkg/mod.py", line=10, title="bug one", detail="explanation one"),
             _finding(file="pkg/other.py", line=None, title="bug two", detail="explanation two")]
    goal = build_fix_goal(group, "C:/fakerepo")

    assert set(goal.keys()) == {"text", "cwd"}
    assert goal["cwd"] == "C:/fakerepo"
    assert "checks" not in goal
    assert FINDINGS_BEGIN in goal["text"]
    assert FINDINGS_END in goal["text"]
    assert "DONE" in goal["text"]
    assert "applied" in goal["text"]


def test_build_fix_goal_verbatim_file_and_title_asserts():
    group = [_finding(file="a/weird file.py", line=1, title="タイトルwith unicode 123",
                       detail="d")]
    goal = build_fix_goal(group, "C:/fakerepo")
    assert "a/weird file.py" in goal["text"]
    assert "タイトルwith unicode 123" in goal["text"]


def test_build_fix_goal_multiple_findings_all_appear():
    group = [_finding(file="x.py", line=i, title="finding-%d" % i) for i in range(5)]
    goal = build_fix_goal(group, "C:/fakerepo")
    for f in group:
        assert f["title"] in goal["text"]
        assert f["file"] in goal["text"]


# --- find_latest_report / load_report -----------------------------------------------------

def test_find_latest_report_picks_lexically_last(tmp_path):
    d = str(tmp_path / "reports")
    os.makedirs(d, exist_ok=True)
    for stamp in ["20260101_000000", "20260711_120000", "20260305_093000"]:
        with open(os.path.join(d, "review_report_%s.json" % stamp), "w", encoding="utf-8") as f:
            json.dump({"findings": []}, f)
    latest = find_latest_report(d)
    assert latest is not None
    assert "20260711_120000" in latest


def test_find_latest_report_none_when_empty(tmp_path):
    d = str(tmp_path / "empty_reports")
    os.makedirs(d, exist_ok=True)
    assert find_latest_report(d) is None


def test_find_latest_report_none_when_dir_missing(tmp_path):
    assert find_latest_report(str(tmp_path / "does_not_exist")) is None


def test_load_report_missing_file(tmp_path):
    report = load_report(str(tmp_path / "nope.json"))
    assert report["findings"] == []
    assert "error" in report


def test_load_report_corrupt_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    report = load_report(str(p))
    assert report["findings"] == []
    assert "error" in report


def test_load_report_normalizes_missing_findings_key(tmp_path):
    p = tmp_path / "noish.json"
    p.write_text(json.dumps({"workers_total": 3}), encoding="utf-8")
    report = load_report(str(p))
    assert report["findings"] == []
    assert "error" not in report
    assert report["workers_total"] == 3


def test_load_report_none_path():
    report = load_report(None)
    assert report["findings"] == []
    assert "error" in report


# --- backup_files / manifest / undo: the critical safety path --------------------------------

def test_backup_files_copies_existing_and_manifest_shape(tmp_path):
    repo_root = str(tmp_path / "repo")
    os.makedirs(os.path.join(repo_root, "sub"), exist_ok=True)
    _write(repo_root, "a.py", "ORIGINAL A\n")
    _write(repo_root, "sub/b.py", "ORIGINAL B\n")
    backup_dir = str(tmp_path / "backup_STAMP")

    groups = [[_finding(file="a.py", line=1, title="t1")],
              [_finding(file="sub/b.py", line=2, title="t2")]]
    manifest = backup_files(groups, repo_root, backup_dir, stamp="STAMP", created_at=123.0)

    assert manifest["stamp"] == "STAMP"
    assert manifest["repo_root"] == repo_root
    assert manifest["created_at"] == 123.0
    by_path = {e["path"]: e for e in manifest["files"]}
    assert by_path["a.py"]["backed_up"] is True
    assert by_path["sub/b.py"]["backed_up"] is True
    assert by_path["a.py"]["findings_applied"][0]["title"] == "t1"

    with open(os.path.join(backup_dir, "a.py"), encoding="utf-8") as f:
        assert f.read() == "ORIGINAL A\n"
    with open(os.path.join(backup_dir, "sub", "b.py"), encoding="utf-8") as f:
        assert f.read() == "ORIGINAL B\n"


def test_backup_files_marks_missing_file_as_not_backed_up(tmp_path):
    repo_root = str(tmp_path / "repo2")
    os.makedirs(repo_root, exist_ok=True)
    backup_dir = str(tmp_path / "backup2")

    groups = [[_finding(file="new_file.py", line=None, title="will be created")]]
    manifest = backup_files(groups, repo_root, backup_dir, stamp="S", created_at=1.0)

    entry = manifest["files"][0]
    assert entry["path"] == "new_file.py"
    assert entry["backed_up"] is False
    assert not os.path.exists(os.path.join(backup_dir, "new_file.py"))


def test_write_manifest_and_load_manifest_round_trip(tmp_path):
    backup_dir = str(tmp_path / "backup3")
    manifest = {"stamp": "S", "repo_root": "C:/x", "created_at": 1.0,
                "files": [{"path": "a.py", "backed_up": True, "findings_applied": []}]}
    write_manifest(manifest, backup_dir)
    loaded = load_manifest(backup_dir)
    assert loaded == manifest


def test_undo_restores_byte_exact(tmp_path):
    repo_root = str(tmp_path / "repo3")
    os.makedirs(repo_root, exist_ok=True)
    _write(repo_root, "a.py", "ORIGINAL CONTENT\nline2\n")
    out_dir = str(tmp_path / "out")
    backup_dir = os.path.join(out_dir, "backup_S1")

    groups = [[_finding(file="a.py", line=1, title="t")]]
    manifest = backup_files(groups, repo_root, backup_dir, stamp="S1", created_at=1.0)
    write_manifest(manifest, backup_dir)

    # simulate the fleet "fixing" (corrupting) the file
    _write(repo_root, "a.py", "MODIFIED BY FLEET\n")
    assert open(os.path.join(repo_root, "a.py"), encoding="utf-8").read() == "MODIFIED BY FLEET\n"

    summary = undo("S1", repo_root, out_dir)
    assert "restored" in summary
    with open(os.path.join(repo_root, "a.py"), encoding="utf-8") as f:
        assert f.read() == "ORIGINAL CONTENT\nline2\n"


def test_undo_deletes_file_created_by_the_fix(tmp_path):
    repo_root = str(tmp_path / "repo4")
    os.makedirs(repo_root, exist_ok=True)
    out_dir = str(tmp_path / "out4")
    backup_dir = os.path.join(out_dir, "backup_S2")

    groups = [[_finding(file="brand_new.py", line=None, title="t")]]
    manifest = backup_files(groups, repo_root, backup_dir, stamp="S2", created_at=1.0)
    write_manifest(manifest, backup_dir)
    assert manifest["files"][0]["backed_up"] is False

    # simulate the fleet CREATING the file
    _write(repo_root, "brand_new.py", "new content from fix\n")
    assert os.path.isfile(os.path.join(repo_root, "brand_new.py"))

    summary = undo("S2", repo_root, out_dir)
    assert "deleted" in summary
    assert not os.path.isfile(os.path.join(repo_root, "brand_new.py"))


def test_undo_missing_manifest_graceful(tmp_path):
    repo_root = str(tmp_path / "repo5")
    os.makedirs(repo_root, exist_ok=True)
    out_dir = str(tmp_path / "out5")  # no backup_NOPE dir ever created
    summary = undo("NOPE", repo_root, out_dir)
    assert isinstance(summary, str)
    assert "undo failed" in summary
    assert "NOPE" in summary


def test_undo_corrupt_manifest_graceful(tmp_path):
    repo_root = str(tmp_path / "repo6")
    os.makedirs(repo_root, exist_ok=True)
    out_dir = str(tmp_path / "out6")
    backup_dir = os.path.join(out_dir, "backup_BAD")
    os.makedirs(backup_dir, exist_ok=True)
    with open(os.path.join(backup_dir, "manifest.json"), "w", encoding="utf-8") as f:
        f.write("{not json")

    summary = undo("BAD", repo_root, out_dir)
    assert "undo failed" in summary


def test_write_undo_bat_is_ascii_and_calls_undo(tmp_path):
    repo_root = str(tmp_path / "repo7")
    os.makedirs(repo_root, exist_ok=True)
    out_dir = str(tmp_path / "out7")

    path = write_undo_bat("STAMP123", repo_root, out_dir)
    assert os.path.isfile(path)
    assert path.endswith("undo_STAMP123.bat")

    raw = open(path, "rb").read()
    raw.decode("ascii")  # raises if any non-ASCII byte slipped in

    text = raw.decode("ascii")
    assert "--undo STAMP123" in text
    assert "review_fix.py" in text
    assert "pause" in text.lower()


# --- git_bonus_precheck: the safety table ----------------------------------------------------

def test_git_bonus_precheck_clean_repo_true(repo):
    assert git_bonus_precheck(repo) is True


def test_git_bonus_precheck_dirty_repo_false(repo):
    _write(repo, "a.py", "print(1)\nmore\n")  # unstaged modification -> dirty
    assert git_bonus_precheck(repo) is False


def test_git_bonus_precheck_not_a_repo_false(tmp_path):
    not_repo = str(tmp_path / "plainfolder")
    os.makedirs(not_repo, exist_ok=True)
    assert git_bonus_precheck(not_repo) is False


def test_git_bonus_precheck_git_absent_false(repo, monkeypatch):
    monkeypatch.setattr(review_fix.shutil, "which", lambda name: None)
    assert git_bonus_precheck(repo) is False


# --- main() --dry-run: no backup / no git / no fleet ------------------------------------------

def _write_report(review_dir, stamp, findings):
    os.makedirs(review_dir, exist_ok=True)
    path = os.path.join(review_dir, "review_report_%s.json" % stamp)
    payload = {"workers_total": 1, "parse_errors": 0, "findings": findings,
               "by_severity": {"high": [], "medium": [], "low": []}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return path


def test_main_dry_run_builds_goals_no_backup_no_git_no_fleet(repo, monkeypatch, capsys):
    monkeypatch.setattr(review_fix, "REPO", repo)
    monkeypatch.setattr(review_run, "REPO", repo)
    _no_fleet_guard(monkeypatch)

    review_dir = os.path.join(repo, ".fleet", "review")
    _write_report(review_dir, "20260101_000000",
                  [_finding(file="a.py", line=1, title="bug", severity="high")])

    rc = main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "goals: 1" in out

    out_dir = os.path.join(repo, ".fleet", "review_fix")
    # no backup dir, no manifest, no undo .bat, no fix_report -- dry-run stops before all of it
    if os.path.isdir(out_dir):
        entries = os.listdir(out_dir)
        assert not any(e.startswith("backup_") for e in entries)
        assert not any(e.startswith("fix_report_") for e in entries)
        assert not any(e.startswith("undo_") for e in entries)


def test_main_no_report_found_message(tmp_path, monkeypatch, capsys):
    root = str(tmp_path / "norepo")
    os.makedirs(root, exist_ok=True)
    monkeypatch.setattr(review_fix, "REPO", root)
    monkeypatch.setattr(review_run, "REPO", root)
    _no_fleet_guard(monkeypatch)

    rc = main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no report found, run /review first" in out


def test_main_zero_findings_after_filter(repo, monkeypatch, capsys):
    monkeypatch.setattr(review_fix, "REPO", repo)
    monkeypatch.setattr(review_run, "REPO", repo)
    _no_fleet_guard(monkeypatch)

    review_dir = os.path.join(repo, ".fleet", "review")
    _write_report(review_dir, "20260101_000000",
                  [_finding(file="a.py", line=1, title="minor", severity="low")])

    rc = main(["--min-severity", "high"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "0 findings match, nothing to fix" in out


# --- main() non-dry-run: fake fleet + failing test gate ---------------------------------------

def _fake_fixes_worker_text(fixes):
    body = json.dumps(fixes, ensure_ascii=False)
    return "prose\n" + FINDINGS_BEGIN + "\n" + body + "\n" + FINDINGS_END + "\nDONE"


def test_main_non_dry_run_preserves_backup_and_branch_on_test_failure(repo, monkeypatch, capsys):
    monkeypatch.setattr(review_fix, "REPO", repo)
    monkeypatch.setattr(review_run, "REPO", repo)
    monkeypatch.setattr(review_run, "VENVPY", sys.executable)  # must exist; never invoked
    monkeypatch.setattr(review_fix, "VENVPY", sys.executable)

    review_dir = os.path.join(repo, ".fleet", "review")
    _write_report(review_dir, "20260101_000000",
                  [_finding(file="a.py", line=1, title="fixable bug", severity="high")])

    def _fake_run_fix_fleet(goals_path, max_concurrent, effort):
        fleet_dir = os.path.join(repo, ".fleet")
        os.makedirs(fleet_dir, exist_ok=True)
        # simulate the worker actually editing the file, so there is something for
        # git_bonus_commit to stage/commit (an unchanged "fix" has nothing to commit).
        _write(repo, "a.py", "print(1)  # fixed\n")
        fixes = [{"file": "a.py", "line": 1, "title": "fixable bug",
                   "applied": True, "summary": "fixed it"}]
        worker_text = _fake_fixes_worker_text(fixes)
        status = {"running": False, "workers": [
            {"name": "w1", "goal": "fix a.py", "outcome": "done", "reason": "",
             "verified": True, "transcript": "", "display_result": worker_text},
        ]}
        with open(os.path.join(fleet_dir, "status.json"), "w", encoding="utf-8") as f:
            json.dump(status, f)
        return 0

    monkeypatch.setattr(review_fix, "run_fix_fleet", _fake_run_fix_fleet)
    monkeypatch.setattr(review_fix, "run_test_gate",
                         lambda repo_root: ("FAILED", "2 tests failed"))

    rc = main(["--group-size", "5"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "TEST GATE FAILED" in out
    assert "NOT reverted" in out

    out_dir = os.path.join(repo, ".fleet", "review_fix")
    backup_dirs = [d for d in os.listdir(out_dir) if d.startswith("backup_")]
    assert len(backup_dirs) == 1
    # backup content still there -- proves nothing was auto-reverted
    assert os.path.isfile(os.path.join(out_dir, backup_dirs[0], "a.py"))

    fix_reports_json = [f for f in os.listdir(out_dir)
                         if f.startswith("fix_report_") and f.endswith(".json")]
    assert len(fix_reports_json) == 1
    with open(os.path.join(out_dir, fix_reports_json[0]), encoding="utf-8") as f:
        report = json.load(f)
    assert report["test_result"] == "FAILED"
    assert len(report["findings_applied"]) == 1
    assert report["branch"] is not None  # repo fixture is clean -> git bonus branch created

    fix_reports_md = [f for f in os.listdir(out_dir)
                       if f.startswith("fix_report_") and f.endswith(".md")]
    with open(os.path.join(out_dir, fix_reports_md[0]), encoding="utf-8") as f:
        md = f.read()
    assert "NOT reverted" in md

    # the branch really exists and really has a commit on it (git bonus actually ran)
    r = _run(["git", "branch", "--show-current"], repo)
    assert r.stdout.strip() == report["branch"]
    r2 = _run(["git", "log", "--oneline", "-1"], repo)
    assert "review-fix" in r2.stdout


def test_main_skip_tests_flag(repo, monkeypatch, capsys):
    monkeypatch.setattr(review_fix, "REPO", repo)
    monkeypatch.setattr(review_run, "REPO", repo)
    monkeypatch.setattr(review_run, "VENVPY", sys.executable)
    monkeypatch.setattr(review_fix, "VENVPY", sys.executable)

    review_dir = os.path.join(repo, ".fleet", "review")
    _write_report(review_dir, "20260101_000000",
                  [_finding(file="a.py", line=1, title="fixable bug", severity="high")])

    def _fake_run_fix_fleet(goals_path, max_concurrent, effort):
        fleet_dir = os.path.join(repo, ".fleet")
        os.makedirs(fleet_dir, exist_ok=True)
        with open(os.path.join(fleet_dir, "status.json"), "w", encoding="utf-8") as f:
            json.dump({"running": False, "workers": []}, f)
        return 0

    def _gate_must_not_run(repo_root):
        raise AssertionError("run_test_gate must not be called with --skip-tests")

    monkeypatch.setattr(review_fix, "run_fix_fleet", _fake_run_fix_fleet)
    monkeypatch.setattr(review_fix, "run_test_gate", _gate_must_not_run)

    rc = main(["--skip-tests"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "test_gate=skipped" in out


# --- --undo end-to-end through main() ----------------------------------------------------------

def test_main_undo_end_to_end(tmp_path):
    repo_root = str(tmp_path / "undo_repo")
    os.makedirs(repo_root, exist_ok=True)
    _write(repo_root, "a.py", "ORIGINAL\n")
    out_dir = os.path.join(repo_root, ".fleet", "review_fix")

    from bench.review_fix import backup_files as _bf, write_manifest as _wm
    backup_dir = os.path.join(out_dir, "backup_E2E")
    groups = [[_finding(file="a.py", line=1, title="t")]]
    manifest = _bf(groups, repo_root, backup_dir, stamp="E2E", created_at=1.0)
    _wm(manifest, backup_dir)

    _write(repo_root, "a.py", "CORRUPTED BY FLEET\n")

    import bench.review_fix as rf
    old_repo = rf.REPO
    rf.REPO = repo_root
    try:
        rc = main(["--undo", "E2E", "--out-dir", ".fleet/review_fix"])
    finally:
        rf.REPO = old_repo
    assert rc == 0
    with open(os.path.join(repo_root, "a.py"), encoding="utf-8") as f:
        assert f.read() == "ORIGINAL\n"


# --- argparse smoke ------------------------------------------------------------------------

def test_build_arg_parser_defaults():
    args = build_arg_parser().parse_args([])
    assert args.min_severity == "medium"
    assert args.verified_only is False
    assert args.max_concurrent == 4
    assert args.group_size == 5
    assert args.effort == "auto"
    assert args.dry_run is False
    assert args.skip_tests is False
    assert args.undo is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
