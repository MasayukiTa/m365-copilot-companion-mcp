# -*- coding: utf-8 -*-
"""Lab auto rollback decision (slice F8)."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.org import lab_rollback as R  # noqa: E402

SCRIPT = os.path.join(REPO, "scripts", "org_lab_rollback.py")
H = "a" * 64
GIT_POINT = {"ok": True, "kind": "git", "repo": "/work/tree", "head": "0123456789abcdef0123", "snapshot": "",
             "why": ""}


def run(value, h=H, n=30):
    return {"bundle_hash": h, "scores": {"t%02d" % i: {"pass_rate": value} for i in range(n)}}


def obs(cand=0.9, base=0.9, h=H, n=30):
    return {"candidate": run(cand, h, n), "baseline": run(base, h, n)}


def evidence():
    return {"candidate_id": "c1",
            "promotion": {"candidate_id": "c1", "bundle_hash": H, "restore_point": copy.deepcopy(GIT_POINT)},
            "observations": [obs()],
            "security": {"ok": True, "guards": [], "counts": {"pass": 1},
                         "coverage": {"not_frozen": [], "uncovered": []}}}


def codes(rep):
    return [r["code"] for r in rep["reasons"]]


def test_healthy_candidate_is_kept():
    rep = R.rollback_plan(evidence())
    assert rep["decision"] == "keep" and rep["reasons"] == [] and rep["plan"] is None
    assert rep["candidate_id"] == "c1" and rep["observations"][0]["verdict"] == "pass"


def test_regression_after_promotion_rolls_back_with_the_recorded_point():
    ev = evidence()
    ev["observations"] = [obs(0.2, 0.9)]
    rep = R.rollback_plan(ev)
    assert rep["decision"] == "rollback" and codes(rep) == ["regressed"]
    assert rep["plan"]["steps"] == [{"tool": "roll_back", "argument": GIT_POINT}]
    assert any("revert" in n for n in rep["plan"]["notes"])


def test_failed_security_report_rolls_back():
    ev = evidence()
    ev["security"] = {"ok": False}
    rep = R.rollback_plan(ev)
    assert rep["decision"] == "rollback" and "security_not_ok" in codes(rep)
    assert rep["plan"]["steps"][0]["tool"] == "roll_back"


def test_one_bad_observation_among_good_ones_is_enough():
    ev = evidence()
    ev["observations"] = [obs(), obs(0.1, 0.9), obs()]
    rep = R.rollback_plan(ev)
    assert rep["decision"] == "rollback"
    assert [o["verdict"] for o in rep["observations"]] == ["pass", "fail", "pass"]
    assert rep["reasons"][0]["input"] == "observations[1]"


def test_drop_within_the_canary_tolerance_is_kept():
    ev = evidence()
    ev["observations"] = [obs(0.89, 0.9)]
    assert R.rollback_plan(ev)["decision"] == "keep"


def test_several_observations_all_passing_is_kept():
    ev = evidence()
    ev["observations"] = [obs(), obs(0.95, 0.9)]
    assert R.rollback_plan(ev)["decision"] == "keep"


def test_zip_restore_point_gives_a_manual_note_and_no_step():
    ev = evidence()
    ev["promotion"]["restore_point"] = {"ok": True, "kind": "zip", "snapshot": "snap/before.zip"}
    ev["observations"] = [obs(0.2, 0.9)]
    rep = R.rollback_plan(ev)
    assert rep["decision"] == "rollback" and rep["plan"]["steps"] == []
    assert "snap/before.zip" in rep["plan"]["notes"][0] and "no_restore_point" not in codes(rep)


@pytest.mark.parametrize("point", [None, {"ok": False, "kind": "git", "head": "x"},
                                   {"ok": True, "kind": "git", "head": ""},
                                   {"ok": True, "kind": "zip"},
                                   {"ok": True, "kind": "tar"}, "abc"])
def test_rollback_without_a_usable_restore_point_says_so(point):
    ev = evidence()
    ev["promotion"]["restore_point"] = point
    ev["observations"] = [obs(0.2, 0.9)]
    rep = R.rollback_plan(ev)
    assert rep["decision"] == "rollback" and rep["plan"]["steps"] == []
    assert "no_restore_point" in codes(rep)


@pytest.mark.parametrize("key", R.REQUIRED_KEYS)
def test_every_missing_input_is_undecided(key):
    ev = evidence()
    del ev[key]
    rep = R.rollback_plan(ev)
    assert rep["decision"] == "undecided"
    assert any(r["code"] == "evidence_missing" and r["input"] == key for r in rep["reasons"])


@pytest.mark.parametrize("bad", [None, [], "x", 3])
def test_non_object_evidence_is_undecided_not_an_exception(bad):
    rep = R.rollback_plan(bad)
    assert rep["decision"] == "undecided" and set(codes(rep)) == {"evidence_missing"}
    assert len(rep["reasons"]) == len(R.REQUIRED_KEYS) and rep["candidate_id"] is None


@pytest.mark.parametrize("obs_value", [[], {}, "x", 4])
def test_observations_must_be_a_non_empty_list(obs_value):
    ev = evidence()
    ev["observations"] = obs_value
    rep = R.rollback_plan(ev)
    assert rep["decision"] == "undecided" and codes(rep) == ["evidence_missing"]


def test_null_input_counts_as_missing():
    ev = evidence()
    ev["security"] = None
    assert "evidence_missing" in codes(R.rollback_plan(ev))


def test_bad_candidate_id_type_is_undecided():
    ev = evidence()
    ev["candidate_id"] = 5
    rep = R.rollback_plan(ev)
    assert rep["decision"] == "undecided" and rep["candidate_id"] is None


def test_promotion_record_for_another_candidate_is_invalid():
    ev = evidence()
    ev["promotion"]["candidate_id"] = "other"
    rep = R.rollback_plan(ev)
    assert rep["decision"] == "undecided" and codes(rep) == ["promotion_invalid"]


@pytest.mark.parametrize("promo", ["x", [], 3])
def test_promotion_record_must_be_an_object(promo):
    ev = evidence()
    ev["promotion"] = promo
    assert "promotion_invalid" in codes(R.rollback_plan(ev))


@pytest.mark.parametrize("h", ["abc", "A" * 64, 5, None])
def test_promotion_bundle_hash_must_be_64_hex(h):
    ev = evidence()
    ev["promotion"]["bundle_hash"] = h
    assert "promotion_invalid" in codes(R.rollback_plan(ev))


@pytest.mark.parametrize("bad", [{"candidate": 1, "baseline": 2}, {"candidate": run(0.9)}, "x", None,
                                 {"candidate": {"bundle_hash": H, "scores": {"t": {"m": 7}}},
                                  "baseline": run(0.9)}])
def test_malformed_observation_is_undecided_not_an_exception(bad):
    ev = evidence()
    ev["observations"] = [bad]
    rep = R.rollback_plan(ev)
    assert rep["decision"] == "undecided" and codes(rep) == ["observation_malformed"]
    assert rep["observations"][0]["verdict"] == "malformed"


def test_observation_for_another_bundle_is_undecided_not_a_rollback():
    ev = evidence()
    other = "b" * 64
    ev["observations"] = [obs(0.2, 0.9, h=other)]
    rep = R.rollback_plan(ev)
    assert rep["decision"] == "undecided" and codes(rep) == ["observation_bundle_mismatch"]


def test_runs_naming_different_bundles_are_undecided():
    ev = evidence()
    ev["observations"] = [{"candidate": run(0.9), "baseline": run(0.9, h="b" * 64)}]
    rep = R.rollback_plan(ev)
    assert rep["decision"] == "undecided" and "observation_bundle_mismatch" in codes(rep)


def test_too_few_samples_alone_is_undecided_not_keep():
    ev = evidence()
    ev["observations"] = [obs(n=3)]
    rep = R.rollback_plan(ev)
    assert rep["decision"] == "undecided" and codes(rep) == ["no_passing_observation"]
    assert rep["observations"][0]["verdict"] == "insufficient"


def test_insufficient_next_to_a_pass_is_kept():
    ev = evidence()
    ev["observations"] = [obs(n=3), obs()]
    assert R.rollback_plan(ev)["decision"] == "keep"


def test_metric_missing_in_candidate_is_undecided():
    ev = evidence()
    ev["observations"] = [{"candidate": {"bundle_hash": H, "scores": {"t%02d" % i: {"other": 1.0} for i in range(30)}},
                           "baseline": run(0.9)}]
    rep = R.rollback_plan(ev)
    assert rep["decision"] == "undecided" and "metrics_mismatch" in codes(rep)


def test_measured_regression_wins_over_a_malformed_observation():
    ev = evidence()
    ev["observations"] = ["junk", obs(0.2, 0.9)]
    rep = R.rollback_plan(ev)
    assert rep["decision"] == "rollback"
    assert {"regressed", "observation_malformed"} <= set(codes(rep))


def test_evidence_cannot_loosen_the_rule():
    ev = evidence()
    ev["observations"] = [dict(obs(0.5, 0.9), tolerance=0.5, min_samples=1)]
    ev["tolerance"] = 0.5
    assert R.rollback_plan(ev)["decision"] == "rollback"


def test_input_is_not_mutated_and_report_is_stable_json():
    ev = evidence()
    ev["observations"] = [obs(0.2, 0.9)]
    before = copy.deepcopy(ev)
    rep = R.rollback_plan(ev)
    assert ev == before
    assert json.loads(R.to_json(rep)) == rep and R.to_json(rep) == R.to_json(R.rollback_plan(ev))


def wjson(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")
    return str(path)


def cli(*args):
    p = subprocess.run([sys.executable, SCRIPT] + list(args), capture_output=True, timeout=120)
    return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")


def test_cli_keep_rollback_undecided_and_json(tmp_path):
    rc, out, err = cli("--evidence", wjson(tmp_path / "k.json", evidence()))
    assert rc == 0 and "KEEP" in out, out + err
    ev = evidence()
    ev["observations"] = [obs(0.2, 0.9)]
    rc, out, _ = cli("--evidence", wjson(tmp_path / "r.json", ev))
    assert rc == 1 and "ROLLBACK" in out and "call roll_back" in out and "regressed" in out
    rc, out, _ = cli("--evidence", wjson(tmp_path / "r.json", ev), "--format", "json")
    assert rc == 1 and json.loads(out)["plan"]["steps"][0]["tool"] == "roll_back"
    del ev["security"]
    rc, out, _ = cli("--evidence", wjson(tmp_path / "u.json", {"candidate_id": "c1"}))
    assert rc == 3 and "UNDECIDED" in out and "evidence_missing" in out


def test_cli_refuses_unreadable_input(tmp_path):
    rc, _, err = cli("--evidence", str(tmp_path / "missing.json"))
    assert rc == 2 and "refused" in err
    junk = tmp_path / "j.json"
    junk.write_text("{not json", encoding="utf-8")
    rc, _, err = cli("--evidence", str(junk))
    assert rc == 2 and "refused" in err


def test_cli_does_not_write_anything(tmp_path):
    f = wjson(tmp_path / "e.json", evidence())
    before = sorted(os.listdir(tmp_path))
    cli("--evidence", f)
    assert sorted(os.listdir(tmp_path)) == before


def test_module_does_no_io_and_never_calls_the_rollback_tools():
    src = open(os.path.join(REPO, "relay", "org", "lab_rollback.py"), encoding="utf-8").read()
    body = src.split('"""')[2]
    for word in ("open(", "subprocess", "os.system", "shutil", "socket", "time.", "datetime",
                 "autoloop", "auto_ops", "import tools"):
        assert word not in body, word
