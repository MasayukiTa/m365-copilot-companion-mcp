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
from bench.review_build_goals import GAPS_BEGIN, GAPS_END, REVIEW_DIMENSIONS, dimensions_for_kind
from bench.review_run import (
    behavioral_verify,
    fleet_cmd,
    main,
    merge_verdicts,
    parse_behavior_verdict,
    parse_completeness_gaps,
    plan_goals,
    refute_findings,
    run_completeness_critic,
    run_review_loop,
)
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


# --- parse_behavior_verdict: tolerant reader of the BEHAVIOR_VERDICT contract ----------------

def test_parse_behavior_verdict_reproduced():
    text = "実行しました。\nBEHAVIOR_VERDICT: reproduced\nBEHAVIOR_EVIDENCE: 例外が実際に発生した\nDONE"
    verdict, evidence = parse_behavior_verdict(text)
    assert verdict == "reproduced"
    assert "例外が実際に発生した" in evidence


def test_parse_behavior_verdict_not_reproduced():
    text = "BEHAVIOR_VERDICT: not_reproduced\nBEHAVIOR_EVIDENCE: 実行したが問題は起きなかった\nDONE"
    verdict, evidence = parse_behavior_verdict(text)
    assert verdict == "not_reproduced"
    assert "問題は起きなかった" in evidence


def test_parse_behavior_verdict_inconclusive_explicit():
    text = "BEHAVIOR_VERDICT: inconclusive\nBEHAVIOR_EVIDENCE: 実行環境が無かった\nDONE"
    verdict, evidence = parse_behavior_verdict(text)
    assert verdict == "inconclusive"
    assert "実行環境が無かった" in evidence


def test_parse_behavior_verdict_garbage_folds_to_inconclusive():
    for garbage in ("", "no marker at all here", "BEHAVIOR_VERDICT: bogus_word\nDONE",
                     "some prose mentioning reproduced in passing but no tag"):
        verdict, evidence = parse_behavior_verdict(garbage)
        assert verdict == "inconclusive"


def test_parse_behavior_verdict_never_raises_on_none():
    verdict, evidence = parse_behavior_verdict(None)
    assert verdict == "inconclusive"
    assert evidence == ""


def test_parse_behavior_verdict_case_and_fullwidth_colon_tolerant():
    verdict, _ = parse_behavior_verdict("behavior_verdict：reproduced")
    assert verdict == "reproduced"


def test_parse_behavior_verdict_last_tag_wins():
    # an agent may restate its reasoning before the final answer -- last recognized tag wins
    text = "BEHAVIOR_VERDICT: inconclusive\n(more thinking...)\nBEHAVIOR_VERDICT: reproduced\nDONE"
    verdict, _ = parse_behavior_verdict(text)
    assert verdict == "reproduced"


# --- behavioral_verify(): P2 piece A, mirrors refute_findings' own wiring -------------------

def _fake_behavioral_fleet_factory(status_payload):
    def _fake(goals_path, max_concurrent, effort, state_dir=None):
        assert state_dir, "behavioral_verify must launch its own state_dir'd fleet run"
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "status.json"), "w", encoding="utf-8") as f:
            json.dump(status_payload, f)
        return 0
    return _fake


def test_behavioral_verify_attaches_verdict_only_to_confirmed_findings(tmp_path, monkeypatch):
    findings = [
        {"file": "a.py", "line": 1, "title": "T1", "detail": "d1", "severity": "high",
         "verify_verdict": "confirmed"},
        {"file": "b.py", "line": 2, "title": "T2", "detail": "d2", "severity": "medium",
         "verify_verdict": "false_positive"},
        {"file": "c.py", "line": 3, "title": "T3", "detail": "d3", "severity": "low",
         "verify_verdict": "unclear"},
        {"file": "d.py", "line": 4, "title": "T4", "detail": "d4", "severity": "high",
         "verify_verdict": None},
    ]
    # only findings[0] ("T1") is CONFIRMED -> exactly one goal -> w0.
    status_payload = {"workers": [
        {"name": "w0", "display_result": "実行しました。\nBEHAVIOR_VERDICT: reproduced\n"
                                          "BEHAVIOR_EVIDENCE: 実際に確認できた\nDONE",
         "transcript": ""},
    ]}
    monkeypatch.setattr(review_run, "run_fleet", _fake_behavioral_fleet_factory(status_payload))

    out_dir = str(tmp_path / "out")
    attached = behavioral_verify(findings, out_dir, now=0.0, repo_root=str(tmp_path),
                                  stamp="BSTAMP1")

    assert len(attached) == 1
    assert findings[0]["behavioral_verdict"] == "reproduced"
    assert "実際に確認できた" in findings[0]["behavioral_evidence"]
    # false_positive/unclear/None-verdict findings must NEVER get a behavioral pass
    assert "behavioral_verdict" not in findings[1]
    assert "behavioral_verdict" not in findings[2]
    assert "behavioral_verdict" not in findings[3]

    # exactly one goal was written (only the confirmed finding)
    goals_path = os.path.join(out_dir, "behavioral_goals_BSTAMP1.jsonl")
    with open(goals_path, encoding="utf-8") as f:
        lines = [l for l in f if l.strip()]
    assert len(lines) == 1
    assert "T1" in lines[0]


def test_behavioral_verify_severity_filter(tmp_path, monkeypatch):
    findings = [
        {"file": "a.py", "line": 1, "title": "High one", "detail": "", "severity": "high",
         "verify_verdict": "confirmed"},
        {"file": "b.py", "line": 2, "title": "Low one", "detail": "", "severity": "low",
         "verify_verdict": "confirmed"},
    ]
    status_payload = {"workers": [
        {"name": "w0", "display_result": "BEHAVIOR_VERDICT: reproduced\nDONE", "transcript": ""},
    ]}
    monkeypatch.setattr(review_run, "run_fleet", _fake_behavioral_fleet_factory(status_payload))

    out_dir = str(tmp_path / "out2")
    attached = behavioral_verify(findings, out_dir, now=0.0, repo_root=str(tmp_path),
                                  stamp="BSTAMP2", severity_filter={"high"})

    assert len(attached) == 1
    assert findings[0]["behavioral_verdict"] == "reproduced"  # high one -- selected
    assert "behavioral_verdict" not in findings[1]             # low one -- filtered out

    goals_path = os.path.join(out_dir, "behavioral_goals_BSTAMP2.jsonl")
    with open(goals_path, encoding="utf-8") as f:
        lines = [l for l in f if l.strip()]
    assert len(lines) == 1


def test_behavioral_verify_not_reproduced_verdict(tmp_path, monkeypatch):
    findings = [{"file": "a.py", "line": 1, "title": "T1", "detail": "d1", "severity": "high",
                 "verify_verdict": "confirmed"}]
    status_payload = {"workers": [
        {"name": "w0", "display_result": "実際に実行しましたが問題は再現しませんでした。\n"
                                          "BEHAVIOR_VERDICT: not_reproduced\n"
                                          "BEHAVIOR_EVIDENCE: 入力を与えたが例外は出なかった\nDONE",
         "transcript": ""},
    ]}
    monkeypatch.setattr(review_run, "run_fleet", _fake_behavioral_fleet_factory(status_payload))

    out_dir = str(tmp_path / "out6")
    attached = behavioral_verify(findings, out_dir, now=0.0, repo_root=str(tmp_path),
                                  stamp="BSTAMP6")
    assert len(attached) == 1
    assert findings[0]["behavioral_verdict"] == "not_reproduced"
    assert "例外は出なかった" in findings[0]["behavioral_evidence"]


def test_behavioral_verify_no_confirmed_findings_never_launches_fleet(tmp_path, monkeypatch):
    _no_fleet_guard(monkeypatch)
    findings = [{"file": "a.py", "line": 1, "title": "T", "detail": "", "severity": "low",
                 "verify_verdict": "unclear"}]
    attached = behavioral_verify(findings, str(tmp_path / "out3"), now=0.0,
                                  repo_root=str(tmp_path))
    assert attached == []


def test_behavioral_verify_empty_findings_never_launches_fleet(tmp_path, monkeypatch):
    _no_fleet_guard(monkeypatch)
    attached = behavioral_verify([], str(tmp_path / "out4"), now=0.0, repo_root=str(tmp_path))
    assert attached == []


def test_behavioral_verify_no_status_json_degrades_to_empty(tmp_path, monkeypatch):
    findings = [{"file": "a.py", "line": 1, "title": "T", "detail": "", "severity": "low",
                 "verify_verdict": "confirmed"}]

    def _fake(goals_path, max_concurrent, effort, state_dir=None):
        return 0  # simulate a fleet that never wrote a status.json

    monkeypatch.setattr(review_run, "run_fleet", _fake)
    attached = behavioral_verify(findings, str(tmp_path / "out5"), now=0.0,
                                  repo_root=str(tmp_path), stamp="BSTAMP5")
    assert attached == []
    assert "behavioral_verdict" not in findings[0]


# --- CLI: --behavioral is OFF by default; --behavioral-severity filters --------------------

def test_cli_behavioral_off_by_default_never_launches_second_pass(repo, monkeypatch, capsys):
    monkeypatch.setattr(review_run, "REPO", repo)
    monkeypatch.setattr(review_run, "VENVPY", sys.executable)

    findings_json = json.dumps(
        [{"file": "a.py", "line": 1, "severity": "high", "title": "bad", "detail": "d"}],
        ensure_ascii=False)
    worker_text = (review_run.FINDINGS_BEGIN + "\n" + findings_json + "\n" +
                   review_run.FINDINGS_END + "\nDONE")
    review_status = {"workers": [{"name": "w0", "display_result": worker_text, "transcript": ""}]}
    refute_status = {"workers": [{"name": "w0", "display_result": "UPHELD", "transcript": ""}]}

    calls = []

    def _fake(goals_path, max_concurrent, effort, state_dir=None):
        calls.append(state_dir)
        payload = refute_status if (state_dir and "refute_state" in state_dir) else review_status
        fleet_dir = state_dir or os.path.join(repo, ".fleet")
        os.makedirs(fleet_dir, exist_ok=True)
        with open(os.path.join(fleet_dir, "status.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return 0

    monkeypatch.setattr(review_run, "run_fleet", _fake)

    rc = main(["--kind", "review", "--group-size", "10"])  # --behavioral NOT passed
    assert rc == 0
    # only review pass (state_dir=None) + refute pass (state_dir'd) -- no behavioral_state_*.
    assert not any(sd and "behavioral_state" in sd for sd in calls)

    out = capsys.readouterr().out
    assert "behavioral:" not in out


def test_cli_behavioral_flag_runs_pass_and_marks_demonstrated(repo, monkeypatch, capsys):
    monkeypatch.setattr(review_run, "REPO", repo)
    monkeypatch.setattr(review_run, "VENVPY", sys.executable)

    findings_json = json.dumps(
        [{"file": "a.py", "line": 1, "severity": "high", "title": "bad", "detail": "d"}],
        ensure_ascii=False)
    worker_text = (review_run.FINDINGS_BEGIN + "\n" + findings_json + "\n" +
                   review_run.FINDINGS_END + "\nDONE")
    review_status = {"workers": [{"name": "w0", "display_result": worker_text, "transcript": ""}]}
    refute_status = {"workers": [{"name": "w0", "display_result": "UPHELD", "transcript": ""}]}
    behavioral_status = {"workers": [
        {"name": "w0", "display_result": "BEHAVIOR_VERDICT: reproduced\n"
                                          "BEHAVIOR_EVIDENCE: 実際に実行して確認\nDONE",
         "transcript": ""},
    ]}

    def _fake(goals_path, max_concurrent, effort, state_dir=None):
        if state_dir and "refute_state" in state_dir:
            payload = refute_status
        elif state_dir and "behavioral_state" in state_dir:
            payload = behavioral_status
        else:
            payload = review_status
        fleet_dir = state_dir or os.path.join(repo, ".fleet")
        os.makedirs(fleet_dir, exist_ok=True)
        with open(os.path.join(fleet_dir, "status.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return 0

    monkeypatch.setattr(review_run, "run_fleet", _fake)

    rc = main(["--kind", "review", "--group-size", "10", "--behavioral"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "behavioral: reproduced=1 not_reproduced=0 inconclusive=0" in out

    out_dir = os.path.join(repo, ".fleet", "review")
    json_files = [f for f in os.listdir(out_dir)
                  if f.startswith("review_report_") and f.endswith(".json")]
    assert len(json_files) == 1
    with open(os.path.join(out_dir, json_files[0]), encoding="utf-8") as f:
        report = json.load(f)
    assert report["findings"][0]["behavioral_verdict"] == "reproduced"
    assert report.get("behavioral_summary") == {"reproduced": 1, "not_reproduced": 0,
                                                 "inconclusive": 0}

    md_files = [f for f in os.listdir(out_dir)
                if f.startswith("review_report_") and f.endswith(".md")]
    with open(os.path.join(out_dir, md_files[0]), encoding="utf-8") as f:
        md = f.read()
    assert "DEMONSTRATED" in md
    assert "behavioral verification: reproduced=1" in md


def test_cli_behavioral_severity_filters(repo, monkeypatch, capsys):
    monkeypatch.setattr(review_run, "REPO", repo)
    monkeypatch.setattr(review_run, "VENVPY", sys.executable)

    findings_json = json.dumps([
        {"file": "a.py", "line": 1, "severity": "high", "title": "high one", "detail": "d"},
        {"file": "sub/b.py", "line": 2, "severity": "low", "title": "low one", "detail": "d2"},
    ], ensure_ascii=False)
    worker_text = (review_run.FINDINGS_BEGIN + "\n" + findings_json + "\n" +
                   review_run.FINDINGS_END + "\nDONE")
    review_status = {"workers": [{"name": "w0", "display_result": worker_text, "transcript": ""}]}
    # refuter uphelds both findings (w0 for finding 0, w1 for finding 1)
    refute_status = {"workers": [
        {"name": "w0", "display_result": "UPHELD", "transcript": ""},
        {"name": "w1", "display_result": "UPHELD", "transcript": ""},
    ]}
    behavioral_calls = []

    def _fake(goals_path, max_concurrent, effort, state_dir=None):
        if state_dir and "behavioral_state" in state_dir:
            with open(goals_path, encoding="utf-8") as f:
                behavioral_calls.append([json.loads(l) for l in f if l.strip()])
            payload = {"workers": [
                {"name": "w0", "display_result": "BEHAVIOR_VERDICT: reproduced\nDONE",
                 "transcript": ""},
            ]}
        elif state_dir and "refute_state" in state_dir:
            payload = refute_status
        else:
            payload = review_status
        fleet_dir = state_dir or os.path.join(repo, ".fleet")
        os.makedirs(fleet_dir, exist_ok=True)
        with open(os.path.join(fleet_dir, "status.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return 0

    monkeypatch.setattr(review_run, "run_fleet", _fake)

    rc = main(["--kind", "review", "--group-size", "10", "--behavioral",
               "--behavioral-severity", "high"])
    assert rc == 0

    # only ONE behavioral goal was written (the high-severity finding)
    assert len(behavioral_calls) == 1
    assert len(behavioral_calls[0]) == 1
    assert "high one" in behavioral_calls[0][0]["text"]
    assert "low one" not in behavioral_calls[0][0]["text"]


# --- parse_completeness_gaps: the one place the GAPS_BEGIN/GAPS_END contract is parsed -----

def _gaps_block(missing_dimensions=None, missing_files=None, unverified_claims=None):
    payload = {
        "missing_dimensions": missing_dimensions or [],
        "missing_files": missing_files or [],
        "unverified_claims": unverified_claims or [],
    }
    return GAPS_BEGIN + "\n" + json.dumps(payload, ensure_ascii=False) + "\n" + GAPS_END


def test_parse_completeness_gaps_valid():
    text = "確認しました。\n" + _gaps_block(
        missing_dimensions=["test_hygiene"], missing_files=["c.py"],
        unverified_claims=["claim X was never actually checked"]) + "\nDONE"
    gaps = parse_completeness_gaps(text)
    assert gaps == {
        "missing_dimensions": ["test_hygiene"],
        "missing_files": ["c.py"],
        "unverified_claims": ["claim X was never actually checked"],
    }


def test_parse_completeness_gaps_all_empty():
    text = _gaps_block() + "\nDONE"
    gaps = parse_completeness_gaps(text)
    assert gaps == {"missing_dimensions": [], "missing_files": [], "unverified_claims": []}


def test_parse_completeness_gaps_missing_block_returns_empty():
    gaps = parse_completeness_gaps("no gaps block here at all, just prose. DONE")
    assert gaps == {"missing_dimensions": [], "missing_files": [], "unverified_claims": []}


def test_parse_completeness_gaps_malformed_json_returns_empty():
    text = GAPS_BEGIN + "\n{not valid json}\n" + GAPS_END
    gaps = parse_completeness_gaps(text)
    assert gaps == {"missing_dimensions": [], "missing_files": [], "unverified_claims": []}


def test_parse_completeness_gaps_not_an_object_returns_empty():
    text = GAPS_BEGIN + "\n[1, 2, 3]\n" + GAPS_END
    gaps = parse_completeness_gaps(text)
    assert gaps == {"missing_dimensions": [], "missing_files": [], "unverified_claims": []}


def test_parse_completeness_gaps_partial_fields_default_to_empty():
    text = GAPS_BEGIN + '\n{"missing_dimensions": ["security"]}\n' + GAPS_END
    gaps = parse_completeness_gaps(text)
    assert gaps == {"missing_dimensions": ["security"], "missing_files": [],
                     "unverified_claims": []}


def test_parse_completeness_gaps_never_raises_on_none_or_empty():
    assert parse_completeness_gaps(None) == {
        "missing_dimensions": [], "missing_files": [], "unverified_claims": []}
    assert parse_completeness_gaps("") == {
        "missing_dimensions": [], "missing_files": [], "unverified_claims": []}


# --- run_completeness_critic: P3 piece B, own state_dir'd single-goal fleet pass -----------

def _fake_completeness_fleet_factory(status_payload):
    def _fake(goals_path, max_concurrent, effort, state_dir=None):
        assert state_dir, "run_completeness_critic must launch its own state_dir'd fleet run"
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, "status.json"), "w", encoding="utf-8") as f:
            json.dump(status_payload, f)
        return 0
    return _fake


def test_run_completeness_critic_parses_gaps_from_worker(tmp_path, monkeypatch):
    status_payload = {"workers": [
        {"name": "w0", "display_result": "見てきました。\n" + _gaps_block(
            missing_dimensions=["test_hygiene"], missing_files=["untouched.py"]) + "\nDONE",
         "transcript": ""},
    ]}
    monkeypatch.setattr(review_run, "run_fleet", _fake_completeness_fleet_factory(status_payload))

    gaps = run_completeness_critic(["correctness"], ["a.py"], [], str(tmp_path / "out"),
                                    str(tmp_path), stamp="CSTAMP1")
    assert gaps["missing_dimensions"] == ["test_hygiene"]
    assert gaps["missing_files"] == ["untouched.py"]

    goals_path = os.path.join(str(tmp_path / "out"), "completeness_goals_CSTAMP1.jsonl")
    with open(goals_path, encoding="utf-8") as f:
        lines = [l for l in f if l.strip()]
    assert len(lines) == 1  # exactly one goal, per the spec ("spawn ONE critic goal")


def test_run_completeness_critic_no_status_json_degrades_to_empty(tmp_path, monkeypatch):
    def _fake(goals_path, max_concurrent, effort, state_dir=None):
        return 0  # simulate a fleet that never wrote a status.json
    monkeypatch.setattr(review_run, "run_fleet", _fake)

    gaps = run_completeness_critic([], [], [], str(tmp_path / "out2"), str(tmp_path),
                                    stamp="CSTAMP2")
    assert gaps == {"missing_dimensions": [], "missing_files": [], "unverified_claims": []}


def test_run_completeness_critic_no_workers_degrades_to_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(review_run, "run_fleet",
                         _fake_completeness_fleet_factory({"workers": []}))
    gaps = run_completeness_critic([], [], [], str(tmp_path / "out3"), str(tmp_path),
                                    stamp="CSTAMP3")
    assert gaps == {"missing_dimensions": [], "missing_files": [], "unverified_claims": []}


# --- plan_goals(): extra_files restricts to a given subset (additive, P3 support) -----------

def test_plan_goals_extra_files_restricts_to_given_files(repo):
    files, groups, goals, goal_meta = plan_goals(
        "review", "all", repo, group_size=20, dimension_keys=["correctness"],
        extra_files=["a.py"])
    assert files == ["a.py"]
    assert all(m["files"] == ["a.py"] for m in goal_meta)


def test_plan_goals_extra_files_falsy_is_a_no_op(repo):
    files_a, _, _, _ = plan_goals("review", "all", repo, dimension_keys=["correctness"],
                                   extra_files=None)
    files_b, _, _, _ = plan_goals("review", "all", repo, dimension_keys=["correctness"],
                                   extra_files=[])
    files_c, _, _, _ = plan_goals("review", "all", repo, dimension_keys=["correctness"])
    assert files_a == files_b == files_c == sorted(["a.py", "sub/b.py"])


# --- run_review_loop(): P3 piece A, loop-until-dry --------------------------------------------

def _findings_worker_text(items):
    findings_json = json.dumps(items, ensure_ascii=False)
    return (review_run.FINDINGS_BEGIN + "\n" + findings_json + "\n" + review_run.FINDINGS_END +
            "\nDONE")


def test_loop_stops_after_k_dry_rounds(repo, monkeypatch):
    """Every round's mock review pass returns the SAME finding -> after round 1 it is "new",
    every later round it is deduped ("not new") -> dry_rounds=2 consecutive dry rounds must
    stop the loop before max_rounds is reached."""
    monkeypatch.setattr(review_run, "REPO", repo)
    same_finding = [{"file": "a.py", "line": 1, "severity": "high", "title": "same bug",
                      "detail": "d"}]
    worker_text = _findings_worker_text(same_finding)
    review_status = {"workers": [{"name": "w0", "display_result": worker_text, "transcript": ""}]}

    def _fake(goals_path, max_concurrent, effort, state_dir=None):
        fleet_dir = state_dir or os.path.join(repo, ".fleet")
        os.makedirs(fleet_dir, exist_ok=True)
        with open(os.path.join(fleet_dir, "status.json"), "w", encoding="utf-8") as f:
            json.dump(review_status, f)
        return 0

    monkeypatch.setattr(review_run, "run_fleet", _fake)

    out_dir = os.path.join(repo, ".fleet", "review")
    agg, loop_meta = run_review_loop(
        "review", "all", repo, out_dir, "BASESTAMP", group_size=20,
        dimension_keys=["correctness"], run_refute=False, max_rounds=5, dry_rounds=2)

    # round1: 1 new (consecutive_dry=0); round2: 0 new (consecutive_dry=1);
    # round3: 0 new (consecutive_dry=2) -> stop.
    assert loop_meta["rounds_run"] == 3
    assert loop_meta["stopped_reason"] == "dry"
    assert loop_meta["unique_findings"] == 1
    assert len(agg["findings"]) == 1


def test_loop_stops_at_max_rounds_when_every_round_yields_new_findings(repo, monkeypatch):
    """Every round's mock review pass returns a FRESH, distinct finding (different title each
    time) -> dedup never kicks in -> the loop must run every round up to max_rounds and stop
    there (stopped_reason="max_rounds"), not because it went dry."""
    monkeypatch.setattr(review_run, "REPO", repo)
    counter = {"n": 0}

    def _fake(goals_path, max_concurrent, effort, state_dir=None):
        counter["n"] += 1
        items = [{"file": "a.py", "line": 1, "severity": "high",
                  "title": "distinct bug #%d" % counter["n"], "detail": "d"}]
        payload = {"workers": [
            {"name": "w0", "display_result": _findings_worker_text(items), "transcript": ""}]}
        fleet_dir = state_dir or os.path.join(repo, ".fleet")
        os.makedirs(fleet_dir, exist_ok=True)
        with open(os.path.join(fleet_dir, "status.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return 0

    monkeypatch.setattr(review_run, "run_fleet", _fake)

    out_dir = os.path.join(repo, ".fleet", "review")
    agg, loop_meta = run_review_loop(
        "review", "all", repo, out_dir, "BASESTAMP2", group_size=20,
        dimension_keys=["correctness"], run_refute=False, max_rounds=3, dry_rounds=2)

    assert loop_meta["rounds_run"] == 3
    assert loop_meta["stopped_reason"] == "max_rounds"
    assert loop_meta["unique_findings"] == 3
    assert len(agg["findings"]) == 3


def test_loop_dedup_refuted_finding_does_not_reappear(repo, monkeypatch):
    """A finding REFUTED (false_positive) in round 1 must not reappear in round 2's "new"
    set even though the mock review pass reports the identical finding again -- dedup keys on
    (file, line, title), independent of verify_verdict."""
    monkeypatch.setattr(review_run, "REPO", repo)
    same_finding = [{"file": "a.py", "line": 1, "severity": "high", "title": "flaky claim",
                      "detail": "d"}]
    worker_text = _findings_worker_text(same_finding)
    review_status = {"workers": [{"name": "w0", "display_result": worker_text, "transcript": ""}]}
    refute_status = {"workers": [
        {"name": "w0", "display_result": "REFUTED: not actually a bug", "transcript": ""}]}

    def _fake(goals_path, max_concurrent, effort, state_dir=None):
        payload = refute_status if (state_dir and "refute_state" in state_dir) else review_status
        fleet_dir = state_dir or os.path.join(repo, ".fleet")
        os.makedirs(fleet_dir, exist_ok=True)
        with open(os.path.join(fleet_dir, "status.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return 0

    monkeypatch.setattr(review_run, "run_fleet", _fake)

    out_dir = os.path.join(repo, ".fleet", "review")
    agg, loop_meta = run_review_loop(
        "review", "all", repo, out_dir, "BASESTAMP3", group_size=20,
        dimension_keys=["correctness"], run_refute=True, max_rounds=5, dry_rounds=2)

    # round1: 1 new (refuted); round2: 0 new (deduped, even though refuted last round);
    # round3: 0 new -> dry_rounds=2 reached -> stop at round 3.
    assert loop_meta["rounds_run"] == 3
    assert loop_meta["stopped_reason"] == "dry"
    assert loop_meta["unique_findings"] == 1
    assert len(agg["findings"]) == 1
    assert agg["findings"][0]["verify_verdict"] == "false_positive"


def test_loop_no_files_stops_immediately(tmp_path, monkeypatch):
    root = str(tmp_path / "emptyrepo")
    os.makedirs(root, exist_ok=True)
    _init_repo(root)  # nothing committed -> enumerate_files returns []
    monkeypatch.setattr(review_run, "REPO", root)

    def _boom(*a, **kw):
        raise AssertionError("run_fleet must not be called when there are no files")
    monkeypatch.setattr(review_run, "run_fleet", _boom)

    agg, loop_meta = run_review_loop("review", "all", root, str(tmp_path / "out"), "STAMP",
                                      dimension_keys=["correctness"], max_rounds=3)
    assert loop_meta["stopped_reason"] == "no_files"
    assert loop_meta["rounds_run"] == 1
    assert agg["findings"] == []


def test_loop_fleet_failure_stops_early(repo, monkeypatch):
    monkeypatch.setattr(review_run, "REPO", repo)

    def _fake_no_status(goals_path, max_concurrent, effort, state_dir=None):
        return 0  # never writes status.json

    monkeypatch.setattr(review_run, "run_fleet", _fake_no_status)
    out_dir = os.path.join(repo, ".fleet", "review")
    agg, loop_meta = run_review_loop("review", "all", repo, out_dir, "STAMP2", group_size=20,
                                      dimension_keys=["correctness"], max_rounds=3)
    assert loop_meta["stopped_reason"] == "fleet_failure"
    assert loop_meta["rounds_run"] == 1


def test_loop_completeness_seeds_next_round_dimensions(repo, monkeypatch):
    """completeness=True: the critic's "missing_dimensions" (validated against
    dimensions_for_kind) get unioned into the NEXT round's plan_goals dimension_keys."""
    monkeypatch.setattr(review_run, "REPO", repo)
    round_dims_seen = []

    def _fake(goals_path, max_concurrent, effort, state_dir=None):
        if state_dir and "completeness_state" in state_dir:
            payload = {"workers": [
                {"name": "w0", "display_result": "\n" + _gaps_block(
                    missing_dimensions=["test_hygiene"]) + "\nDONE", "transcript": ""}]}
        else:
            with open(goals_path, encoding="utf-8") as f:
                goal_lines = [json.loads(l) for l in f if l.strip()]
            dims_this_round = set()
            for g in goal_lines:
                if "test_hygiene" in g["text"]:
                    dims_this_round.add("test_hygiene")
                if "correctness" in g["text"]:
                    dims_this_round.add("correctness")
            round_dims_seen.append(dims_this_round)
            payload = {"workers": []}  # no findings -> round goes dry immediately
        fleet_dir = state_dir or os.path.join(repo, ".fleet")
        os.makedirs(fleet_dir, exist_ok=True)
        with open(os.path.join(fleet_dir, "status.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return 0

    monkeypatch.setattr(review_run, "run_fleet", _fake)

    out_dir = os.path.join(repo, ".fleet", "review")
    agg, loop_meta = run_review_loop(
        "review", "all", repo, out_dir, "STAMP3", group_size=20,
        dimension_keys=["correctness"], run_refute=False, completeness=True,
        max_rounds=3, dry_rounds=2)

    # round 1 was scoped to just "correctness" (no test_hygiene yet).
    assert "test_hygiene" not in round_dims_seen[0]
    assert "correctness" in round_dims_seen[0]
    # round 2 must have been seeded with the critic's suggested "test_hygiene" dimension too.
    assert len(round_dims_seen) >= 2
    assert "test_hygiene" in round_dims_seen[1]
    assert agg.get("completeness_gaps") is not None


# --- CLI: --loop / --completeness wiring in main() ------------------------------------------

def test_cli_loop_flag_writes_loop_meta_into_report(repo, monkeypatch, capsys):
    monkeypatch.setattr(review_run, "REPO", repo)
    monkeypatch.setattr(review_run, "VENVPY", sys.executable)

    same_finding = [{"file": "a.py", "line": 1, "severity": "high", "title": "loop finding",
                      "detail": "d"}]
    worker_text = _findings_worker_text(same_finding)
    review_status = {"workers": [{"name": "w0", "display_result": worker_text, "transcript": ""}]}

    def _fake(goals_path, max_concurrent, effort, state_dir=None):
        fleet_dir = state_dir or os.path.join(repo, ".fleet")
        os.makedirs(fleet_dir, exist_ok=True)
        with open(os.path.join(fleet_dir, "status.json"), "w", encoding="utf-8") as f:
            json.dump(review_status, f)
        return 0

    monkeypatch.setattr(review_run, "run_fleet", _fake)

    rc = main(["--kind", "review", "--group-size", "10", "--no-refute", "--loop",
               "--max-rounds", "5", "--dry-rounds", "1"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "launching review LOOP" in out
    assert "loop: rounds_run=2/5 stopped_reason=dry unique_findings=1" in out

    out_dir = os.path.join(repo, ".fleet", "review")
    json_files = [f for f in os.listdir(out_dir)
                  if f.startswith("review_report_") and f.endswith(".json")]
    assert len(json_files) == 1
    with open(os.path.join(out_dir, json_files[0]), encoding="utf-8") as f:
        report = json.load(f)
    assert report["loop_meta"]["rounds_run"] == 2
    assert report["loop_meta"]["stopped_reason"] == "dry"
    assert len(report["findings"]) == 1

    md_files = [f for f in os.listdir(out_dir)
                if f.startswith("review_report_") and f.endswith(".md")]
    with open(os.path.join(out_dir, md_files[0]), encoding="utf-8") as f:
        md = f.read()
    assert "loop: 2/5 round(s) run, stopped: dry" in md


def test_cli_without_loop_or_completeness_report_has_no_p3_keys(repo, monkeypatch, capsys):
    """Regression guard for Piece C: when neither --loop nor --completeness is passed, the
    rendered report must carry none of the new P3 keys/lines at all."""
    monkeypatch.setattr(review_run, "REPO", repo)
    monkeypatch.setattr(review_run, "VENVPY", sys.executable)

    findings_json = json.dumps(
        [{"file": "a.py", "line": 1, "severity": "high", "title": "bad", "detail": "d"}],
        ensure_ascii=False)
    worker_text = (review_run.FINDINGS_BEGIN + "\n" + findings_json + "\n" +
                   review_run.FINDINGS_END + "\nDONE")
    status_payload = {"workers": [{"name": "w0", "display_result": worker_text, "transcript": ""}]}
    monkeypatch.setattr(review_run, "run_fleet",
                         _fake_run_fleet_factory(repo, status_payload))

    rc = main(["--kind", "review", "--group-size", "10", "--no-refute"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "loop:" not in out
    assert "completeness" not in out

    out_dir = os.path.join(repo, ".fleet", "review")
    json_files = [f for f in os.listdir(out_dir)
                  if f.startswith("review_report_") and f.endswith(".json")]
    with open(os.path.join(out_dir, json_files[0]), encoding="utf-8") as f:
        report = json.load(f)
    assert "loop_meta" not in report
    assert "completeness_gaps" not in report

    md_files = [f for f in os.listdir(out_dir)
                if f.startswith("review_report_") and f.endswith(".md")]
    with open(os.path.join(out_dir, md_files[0]), encoding="utf-8") as f:
        md = f.read()
    assert "loop" not in md.lower()
    assert "completeness" not in md.lower()


def test_cli_completeness_flag_without_loop_runs_one_critic_pass(repo, monkeypatch, capsys):
    monkeypatch.setattr(review_run, "REPO", repo)
    monkeypatch.setattr(review_run, "VENVPY", sys.executable)

    findings_json = json.dumps(
        [{"file": "a.py", "line": 1, "severity": "high", "title": "bad", "detail": "d"}],
        ensure_ascii=False)
    worker_text = (review_run.FINDINGS_BEGIN + "\n" + findings_json + "\n" +
                   review_run.FINDINGS_END + "\nDONE")
    review_status = {"workers": [{"name": "w0", "display_result": worker_text, "transcript": ""}]}
    completeness_status = {"workers": [
        {"name": "w0", "display_result": "\n" + _gaps_block(
            missing_dimensions=["test_hygiene"], missing_files=["untouched.py"],
            unverified_claims=["bad finding on a.py:1 was never actually checked"]) + "\nDONE",
         "transcript": ""},
    ]}

    def _fake(goals_path, max_concurrent, effort, state_dir=None):
        if state_dir and "completeness_state" in state_dir:
            payload = completeness_status
        else:
            payload = review_status
        fleet_dir = state_dir or os.path.join(repo, ".fleet")
        os.makedirs(fleet_dir, exist_ok=True)
        with open(os.path.join(fleet_dir, "status.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return 0

    monkeypatch.setattr(review_run, "run_fleet", _fake)

    rc = main(["--kind", "review", "--group-size", "10", "--no-refute", "--completeness"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "completeness critic reported gaps" in out

    out_dir = os.path.join(repo, ".fleet", "review")
    json_files = [f for f in os.listdir(out_dir)
                  if f.startswith("review_report_") and f.endswith(".json")]
    with open(os.path.join(out_dir, json_files[0]), encoding="utf-8") as f:
        report = json.load(f)
    assert report["completeness_gaps"]["missing_dimensions"] == ["test_hygiene"]
    assert "loop_meta" not in report  # --completeness alone must not fabricate loop_meta

    md_files = [f for f in os.listdir(out_dir)
                if f.startswith("review_report_") and f.endswith(".md")]
    with open(os.path.join(out_dir, md_files[0]), encoding="utf-8") as f:
        md = f.read()
    assert "completeness critic:" in md
    assert "test_hygiene" in md
    assert "untouched.py" in md


def test_cli_dry_run_ignores_loop_and_completeness(repo, monkeypatch, capsys):
    monkeypatch.setattr(review_run, "REPO", repo)
    _no_fleet_guard(monkeypatch)

    rc = main(["--kind", "review", "--dry-run", "--group-size", "20", "--loop",
               "--completeness"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "ignored under --dry-run" in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
