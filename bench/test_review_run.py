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
from bench.review_build_goals import REVIEW_DIMENSIONS, dimensions_for_kind
from bench.review_run import fleet_cmd, main, merge_verdicts, plan_goals, refute_findings
from relay.fleet_runner import _read_goals_file
from relay.refuter import PANEL_LENSES

REVIEW_DIM_COUNT = len(dimensions_for_kind("review"))
SECURITY_DIM_COUNT = len(dimensions_for_kind("security"))


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
    # a.py, sub/b.py each in their own file-group (group-size 1), fanned out across every
    # review-applicable dimension -> 2 groups * REVIEW_DIM_COUNT dimensions.
    expected_goals = 2 * REVIEW_DIM_COUNT
    assert "file groups: 2" in out
    assert ("goals: %d" % expected_goals) in out

    goals_dir = os.path.join(repo, ".fleet", "review")
    goal_files = [f for f in os.listdir(goals_dir) if f.startswith("goals_")]
    assert len(goal_files) == 1
    goals_path = os.path.join(goals_dir, goal_files[0])
    with open(goals_path, encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert len(lines) == expected_goals

    # the goals file must not trip relay.fleet_runner's fragmented-goals-file guard
    assert len(_read_goals_file(goals_path)) == expected_goals

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
    """Returns a run_fleet stub that writes status_payload as status.json under repo_root's
    .fleet dir (or under state_dir, if the caller -- e.g. refute_findings -- passes one),
    mimicking what a real relay.fleet_runner run leaves behind. Since main() now launches the
    refuter pass BY DEFAULT after the review pass (both routed through the same monkeypatched
    review_run.run_fleet), this must accept the optional state_dir kwarg refute_findings
    passes, not just the review pass's own 3-positional-arg call."""
    def _fake_run_fleet(goals_path, max_concurrent, effort, state_dir=None):
        fleet_dir = state_dir if state_dir else os.path.join(repo_root, ".fleet")
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

    # --no-refute: this test is about the review -> aggregation -> report pipeline, not the
    # refuter pass (covered separately below by the refute_findings() tests).
    rc = main(["--kind", "review", "--group-size", "10", "--no-refute"])
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


# --- refute_findings(): P1b, reuses relay/refuter.py as its own fleet pass ------------------

def _fake_refute_fleet_factory(status_payload):
    """A run_fleet stub for refute_findings' own state_dir'd call -- writes status_payload
    into state_dir/status.json (NOT the shared .fleet dir the review pass uses), mirroring
    what refute_findings expects a real fleet run to leave behind."""
    def _fake(goals_path, max_concurrent, effort, state_dir=None):
        assert state_dir, "refute_findings must launch its own state_dir'd fleet run"
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "status.json"), "w", encoding="utf-8") as f:
            json.dump(status_payload, f)
        return 0
    return _fake


def test_refute_findings_single_lens_maps_verdicts_by_goal_order(tmp_path, monkeypatch):
    findings = [
        {"file": "a.py", "line": 1, "title": "T1", "detail": "d1", "severity": "high"},
        {"file": "b.py", "line": 2, "title": "T2", "detail": "d2", "severity": "medium"},
        {"file": "c.py", "line": 3, "title": "T3", "detail": "d3", "severity": "low"},
    ]
    # w0/w1/w2 -> finding index 0/1/2 (single-lens: one goal per finding, in order).
    status_payload = {"workers": [
        {"name": "w0", "display_result": "確認しました。\nREFUTED: 実際には問題ない", "transcript": ""},
        {"name": "w1", "display_result": "確認済み\nUPHELD", "transcript": ""},
        {"name": "w2", "display_result": "よくわからない出力です garbage", "transcript": ""},
    ]}
    monkeypatch.setattr(review_run, "run_fleet", _fake_refute_fleet_factory(status_payload))

    out_dir = str(tmp_path / "out")
    verdicts = review_run.refute_findings(findings, "review", out_dir, now=0.0, panel=False,
                                          repo_root=str(tmp_path), stamp="STAMP1")

    by_title = {v["title"]: v for v in verdicts}
    assert by_title["T1"]["verdict"] == "false_positive"
    assert "実際には問題ない" in by_title["T1"]["reason"]
    assert by_title["T2"]["verdict"] == "confirmed"
    assert by_title["T3"]["verdict"] == "unclear"

    # single-lens mode writes exactly one goal per finding
    goals_path = os.path.join(out_dir, "refute_goals_STAMP1.jsonl")
    with open(goals_path, encoding="utf-8") as f:
        lines = [l for l in f if l.strip()]
    assert len(lines) == len(findings)

    # end-to-end: merge_verdicts -> review_fix.filter_findings must actually DROP the
    # REFUTED finding, keep the confirmed/unclear ones. This is the whole point of P1b.
    from bench.review_fix import filter_findings
    agg = {"findings": [dict(f) for f in findings]}
    merge_verdicts(agg, verdicts)
    kept_titles = {f["title"] for f in filter_findings(agg["findings"], min_severity="low")}
    assert "T1" not in kept_titles       # REFUTED -> dropped
    assert "T2" in kept_titles           # UPHELD -> confirmed, kept
    assert "T3" in kept_titles           # UNCLEAR is not dropped, only false_positive is


def test_refute_findings_panel_mode_majority_vote(tmp_path, monkeypatch):
    findings = [{"file": "x.py", "line": 9, "title": "Panel finding", "detail": "d",
                 "severity": "high"}]
    # panel mode: 3 goals for this 1 finding (PANEL_LENSES order) -> w0/w1/w2.
    status_payload = {"workers": [
        {"name": "w0", "display_result": "REFUTED: correctness lens reason", "transcript": ""},
        {"name": "w1", "display_result": "REFUTED: edge lens reason", "transcript": ""},
        {"name": "w2", "display_result": "UPHELD", "transcript": ""},
    ]}
    monkeypatch.setattr(review_run, "run_fleet", _fake_refute_fleet_factory(status_payload))

    out_dir = str(tmp_path / "out2")
    verdicts = review_run.refute_findings(findings, "review", out_dir, now=0.0, panel=True,
                                          repo_root=str(tmp_path), stamp="STAMP2")
    assert len(verdicts) == 1
    assert verdicts[0]["verdict"] == "false_positive"  # 2/3 REFUTED -> majority via aggregate_panel

    goals_path = os.path.join(out_dir, "refute_goals_STAMP2.jsonl")
    with open(goals_path, encoding="utf-8") as f:
        lines = [l for l in f if l.strip()]
    assert len(lines) == len(PANEL_LENSES)


def test_refute_findings_no_status_json_degrades_to_empty(tmp_path, monkeypatch):
    findings = [{"file": "a.py", "line": 1, "title": "T", "detail": "", "severity": "low"}]

    def _fake(goals_path, max_concurrent, effort, state_dir=None):
        return 0  # simulate a fleet that never wrote a status.json

    monkeypatch.setattr(review_run, "run_fleet", _fake)
    verdicts = review_run.refute_findings(findings, "review", str(tmp_path / "out3"), now=0.0,
                                          repo_root=str(tmp_path), stamp="STAMP3")
    assert verdicts == []


def test_refute_findings_empty_findings_never_launches_fleet(tmp_path, monkeypatch):
    _no_fleet_guard(monkeypatch)
    verdicts = review_run.refute_findings([], "review", str(tmp_path / "out4"), now=0.0,
                                          repo_root=str(tmp_path))
    assert verdicts == []


def test_cli_no_refute_skips_the_refuter_pass(repo, monkeypatch, capsys):
    monkeypatch.setattr(review_run, "REPO", repo)
    monkeypatch.setattr(review_run, "VENVPY", sys.executable)

    findings_json = json.dumps(
        [{"file": "a.py", "line": 1, "severity": "high", "title": "bad", "detail": "d"}],
        ensure_ascii=False)
    worker_text = (review_run.FINDINGS_BEGIN + "\n" + findings_json + "\n" +
                   review_run.FINDINGS_END + "\nDONE")
    status_payload = {"workers": [
        {"name": "w0", "display_result": worker_text, "transcript": ""},
    ]}

    calls = []

    def _fake(goals_path, max_concurrent, effort, state_dir=None):
        calls.append(state_dir)
        fleet_dir = state_dir or os.path.join(repo, ".fleet")
        os.makedirs(fleet_dir, exist_ok=True)
        with open(os.path.join(fleet_dir, "status.json"), "w", encoding="utf-8") as f:
            json.dump(status_payload, f)
        return 0

    monkeypatch.setattr(review_run, "run_fleet", _fake)

    rc = main(["--kind", "review", "--group-size", "10", "--no-refute"])
    assert rc == 0
    # only the ONE main review-pass run_fleet call (state_dir=None) -- no second, state_dir'd
    # refuter pass call.
    assert calls == [None]

    out = capsys.readouterr().out
    assert "refuter:" not in out


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

    files, groups, goals, goal_meta = plan_goals("review", "diff", repo)
    assert files == ["a.py"]
    assert groups == [["a.py"]]
    # diff mode still fans out across every review dimension for the single group.
    assert len(goals) == REVIEW_DIM_COUNT
    assert len(goal_meta) == REVIEW_DIM_COUNT
    assert {m["dimension"] for m in goal_meta} == {d["key"] for d in dimensions_for_kind("review")}
    assert all(m["files"] == ["a.py"] for m in goal_meta)


# --- plan_goals(): multi-dimensional fan-out -------------------------------------------------

def test_plan_goals_review_kind_uses_only_review_dimensions(repo):
    files, groups, goals, goal_meta = plan_goals("review", "all", repo, group_size=20)
    dims_seen = {m["dimension"] for m in goal_meta}
    review_keys = {d["key"] for d in dimensions_for_kind("review")}
    security_only_keys = {d["key"] for d in REVIEW_DIMENSIONS} - review_keys
    assert dims_seen == review_keys
    assert not (dims_seen & security_only_keys)
    assert len(goals) == len(groups) * REVIEW_DIM_COUNT


def test_plan_goals_security_kind_uses_only_security_dimensions(repo):
    files, groups, goals, goal_meta = plan_goals("security", "all", repo, group_size=20)
    dims_seen = {m["dimension"] for m in goal_meta}
    security_keys = {d["key"] for d in dimensions_for_kind("security")}
    review_only_keys = {d["key"] for d in REVIEW_DIMENSIONS} - security_keys
    assert dims_seen == security_keys
    assert not (dims_seen & review_only_keys)
    assert len(goals) == len(groups) * SECURITY_DIM_COUNT


def test_plan_goals_dimensions_filter_scopes_to_requested_keys(repo):
    files, groups, goals, goal_meta = plan_goals(
        "review", "all", repo, group_size=20, dimension_keys=["correctness", "test_hygiene"])
    assert {m["dimension"] for m in goal_meta} == {"correctness", "test_hygiene"}
    assert len(goals) == len(groups) * 2


def test_plan_goals_dimensions_filter_unknown_key_raises(repo):
    with pytest.raises(ValueError):
        plan_goals("review", "all", repo, dimension_keys=["not_a_real_dimension"])


def test_plan_goals_dimensions_filter_inapplicable_key_raises(repo):
    # "security" dimension key exists but does not apply to kind="review"
    with pytest.raises(ValueError):
        plan_goals("review", "all", repo, dimension_keys=["security"])


def test_plan_goals_no_files_yields_no_goals_regardless_of_dimensions(tmp_path):
    root = str(tmp_path / "emptyrepo")
    os.makedirs(root, exist_ok=True)
    _init_repo(root)
    files, groups, goals, goal_meta = plan_goals("review", "all", root)
    assert files == []
    assert groups == []
    assert goals == []
    assert goal_meta == []


# --- --dimensions CLI filter ------------------------------------------------------------------

def test_cli_dimensions_filter_scopes_dry_run(repo, monkeypatch, capsys):
    monkeypatch.setattr(review_run, "REPO", repo)
    _no_fleet_guard(monkeypatch)

    rc = main(["--kind", "review", "--dry-run", "--group-size", "20",
               "--dimensions", "correctness,missing_wiring"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dimensions: correctness, missing_wiring" in out
    assert "goals: 2" in out  # 1 file group * 2 requested dimensions


# --- concurrency clamp -------------------------------------------------------------------------

def test_max_concurrent_clamped_to_ceiling(repo, monkeypatch, capsys):
    monkeypatch.setattr(review_run, "REPO", repo)
    monkeypatch.setattr(review_run, "MAX_CONCURRENT_CEILING", 5)
    _no_fleet_guard(monkeypatch)

    rc = main(["--kind", "review", "--dry-run", "--max-concurrent", "999"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "'--max-concurrent', '5'" in out
    assert "clamped: requested=999 effective=5" in out


def test_max_concurrent_within_ceiling_unclamped(repo, monkeypatch, capsys):
    monkeypatch.setattr(review_run, "REPO", repo)
    monkeypatch.setattr(review_run, "MAX_CONCURRENT_CEILING", 8)
    _no_fleet_guard(monkeypatch)

    rc = main(["--kind", "review", "--dry-run", "--max-concurrent", "3"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "'--max-concurrent', '3'" in out
    assert "clamped" not in out


# --- dimension coverage note in the rendered report --------------------------------------------

def test_report_notes_dimension_coverage(repo, monkeypatch, capsys):
    monkeypatch.setattr(review_run, "REPO", repo)
    monkeypatch.setattr(review_run, "VENVPY", sys.executable)  # must exist; never invoked
    monkeypatch.setattr(review_run, "run_fleet",
                         _fake_run_fleet_factory(repo, {"running": False, "workers": []}))

    rc = main(["--kind", "review", "--group-size", "10", "--dimensions", "correctness"])
    assert rc == 0

    out_dir = os.path.join(repo, ".fleet", "review")
    md_files = [f for f in os.listdir(out_dir)
                if f.startswith("review_report_") and f.endswith(".md")]
    json_files = [f for f in os.listdir(out_dir)
                  if f.startswith("review_report_") and f.endswith(".json")]
    assert len(md_files) == 1 and len(json_files) == 1

    with open(os.path.join(out_dir, md_files[0]), encoding="utf-8") as f:
        md = f.read()
    assert "dimensions covered: correctness" in md

    with open(os.path.join(out_dir, json_files[0]), encoding="utf-8") as f:
        report = json.load(f)
    assert report["dimensions_covered"] == ["correctness"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
