# -*- coding: utf-8 -*-
"""Lab candidate count caps and sweep plan (slice F2)."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.org import lab_candidates as C  # noqa: E402

SCRIPT = os.path.join(REPO, "scripts", "org_lab_candidates.py")
NOW = "2026-10-22T12:00:00Z"
POLICY = {"max_active": 2, "max_total": 4, "max_age_hours": 24}


@pytest.fixture
def lab(tmp_path):
    d = tmp_path / "lab"
    d.mkdir()
    return str(d)


def rec(lab, n, state="rejected", created="2026-10-22T11:00:00Z", **kw):
    r = {"id": "c%d" % n, "branch": "lab/c%d" % n, "worktree": os.path.join(lab, "c%d" % n),
         "created_at": created, "state": state}
    r.update(kw)
    return r


def plan(lab, recs, policy=None):
    return C.plan_sweep(recs, policy or POLICY, lab, NOW)


def codes(p):
    return [x["code"] for x in p["violations"]]


def test_within_caps_is_ok(lab):
    p = plan(lab, [rec(lab, 1, "active"), rec(lab, 2, "merged")])
    assert p["ok"] and p["sweepable"] == [] and p["over_cap"] == []


def test_active_over_cap_is_reported_and_never_swept(lab):
    p = plan(lab, [rec(lab, i, "active", created="2026-01-01T00:00:00Z") for i in (1, 2, 3)])
    assert not p["ok"]
    assert p["over_cap"] == [{"cap": "max_active", "count": 3, "limit": 2, "excess": 1}]
    assert p["sweepable"] == []


def test_old_non_active_is_sweepable_old_active_is_not(lab):
    old = "2026-10-20T00:00:00Z"
    p = plan(lab, [rec(lab, 1, "active", created=old), rec(lab, 2, "rejected", created=old),
                   rec(lab, 3, "rejected")])
    assert [x["id"] for x in p["sweepable"]] == ["c2"]
    assert p["sweepable"][0]["reason"] == "older_than_max_age"


def test_total_over_cap_sweeps_oldest_non_active_only_as_needed(lab):
    recs = [rec(lab, i, "merged", created="2026-10-22T0%d:00:00Z" % i) for i in range(1, 7)]
    p = plan(lab, recs)
    assert [x["id"] for x in p["sweepable"]] == ["c1", "c2"]
    assert {x["reason"] for x in p["sweepable"]} == {"over_max_total"}
    assert p["unresolved_excess"] == 0
    assert p["over_cap"][0]["cap"] == "max_total" and not p["ok"]


def test_unresolved_excess_when_only_active_remain(lab):
    recs = [rec(lab, i, "active") for i in range(1, 6)]
    p = plan(lab, recs, dict(POLICY, max_active=9))
    assert p["sweepable"] == [] and p["unresolved_excess"] == 1


def test_age_sweep_counts_toward_total(lab):
    old = "2026-10-01T00:00:00Z"
    recs = [rec(lab, 1, created=old), rec(lab, 2, created=old)] + [rec(lab, i) for i in (3, 4, 5)]
    p = plan(lab, recs)
    assert [x["reason"] for x in p["sweepable"]] == ["older_than_max_age"] * 2
    assert p["unresolved_excess"] == 0


def test_branch_prefix_violation(lab):
    p = plan(lab, [rec(lab, 1, branch="main"), rec(lab, 2, branch="lab/")])
    assert codes(p).count("branch_prefix") == 2 and not p["ok"]


def test_worktree_outside_lab_and_production_like_tree(lab, tmp_path):
    prod = tmp_path / "prod"
    prod.mkdir()
    p = plan(lab, [rec(lab, 1, worktree=str(prod / "c1")), rec(lab, 2, worktree=lab)])
    assert sorted(codes(p)) == ["worktree_is_lab_root", "worktree_outside_lab"]


def test_sibling_with_common_name_prefix_is_outside(lab):
    p = plan(lab, [rec(lab, 1, worktree=lab + "2" + os.sep + "c1")])
    assert codes(p) == ["worktree_outside_lab"]


def test_relative_worktree_is_a_violation(lab):
    p = plan(lab, [rec(lab, 1, worktree="c1")])
    assert codes(p) == ["worktree_not_absolute"]


def test_duplicates(lab):
    p = plan(lab, [rec(lab, 1), rec(lab, 1, branch="lab/x", worktree=os.path.join(lab, "x")),
                   rec(lab, 2, branch="lab/c1")])
    assert "duplicate_id" in codes(p) and "duplicate_branch" in codes(p)


def test_plan_is_deterministic_and_input_order_independent(lab):
    recs = [rec(lab, i, "merged", created="2026-10-22T0%d:00:00Z" % i) for i in range(1, 7)]
    a = C.to_json(plan(lab, recs))
    b = C.to_json(plan(lab, list(reversed(recs))))
    assert a == b


@pytest.mark.parametrize("bad", [
    {"state": "weird"}, {"created_at": "yesterday"}, {"id": ""}, {"branch": 3}])
def test_bad_record_is_refused(lab, bad):
    with pytest.raises(C.LabCandidatesError):
        plan(lab, [rec(lab, 1, **bad)])


@pytest.mark.parametrize("bad", [
    {"max_active": -1}, {"max_total": True}, {"max_age_hours": 0}, {"max_age_hours": "x"}])
def test_bad_policy_is_refused(lab, bad):
    with pytest.raises(C.LabCandidatesError):
        plan(lab, [], dict(POLICY, **bad))


def test_naive_timestamp_counts_as_utc(lab):
    p = plan(lab, [rec(lab, 1, created="2026-10-20T00:00:00")])
    assert [x["id"] for x in p["sweepable"]] == ["c1"]


def test_module_neither_deletes_nor_runs_git():
    src = open(os.path.join(REPO, "relay", "org", "lab_candidates.py"), encoding="utf-8").read()
    for banned in ("subprocess", "os.remove", "os.rmdir", "shutil", "rmtree", "os.system",
                   "open(", "mkdir"):
        assert banned not in src, banned


def run_cli(tmp_path, lab, recs, extra=()):
    f = tmp_path / "records.json"
    f.write_text(json.dumps(recs), encoding="utf-8")
    cmd = [sys.executable, "-I", "-X", "utf8", SCRIPT, "--check", "--records", str(f),
           "--lab-root", lab, "--max-active", "2", "--max-total", "4",
           "--max-age-hours", "24", "--now", NOW] + list(extra)
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")


def test_cli_exit_codes(tmp_path, lab):
    ok = run_cli(tmp_path, lab, [rec(lab, 1, "active")])
    assert ok.returncode == 0 and "ok" in ok.stdout
    bad = run_cli(tmp_path, lab, [rec(lab, 1, branch="main")], ["--format", "json"])
    assert bad.returncode == 1
    assert json.loads(bad.stdout)["violations"][0]["code"] == "branch_prefix"
    refused = run_cli(tmp_path, lab, [rec(lab, 1, state="nope")])
    assert refused.returncode == 2 and "refused" in refused.stderr


def test_cli_accepts_wrapped_candidates_and_missing_file(tmp_path, lab):
    f = tmp_path / "w.json"
    f.write_text(json.dumps({"candidates": [rec(lab, 1)]}), encoding="utf-8")
    cmd = [sys.executable, "-I", "-X", "utf8", SCRIPT, "--check", "--records", str(f),
           "--lab-root", lab, "--max-active", "2", "--max-total", "4",
           "--max-age-hours", "24", "--now", NOW]
    assert subprocess.run(cmd, capture_output=True).returncode == 0
    cmd[cmd.index(str(f))] = str(tmp_path / "missing.json")
    assert subprocess.run(cmd, capture_output=True).returncode == 2
