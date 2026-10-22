# -*- coding: utf-8 -*-
"""Lab promotion gate (slice F7)."""
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

from relay.org import lab_bench_bundle as B  # noqa: E402
from relay.org import lab_promotion_gate as G  # noqa: E402

SCRIPT = os.path.join(REPO, "scripts", "org_lab_promotion_gate.py")


def bundle():
    return B.build_bundle({
        "experiment_id": "exp-20261022-a1b2c3",
        "candidate_hash": "%064x" % 0xabc, "baseline_hash": "%064x" % 0xdef,
        "evaluator": {"id": "frozen-harness", "version": "3"},
        "model": {"id": "model-x-2026-09-01"}, "seed": 7,
        "pools": {"evolution": ["t1", "t2"], "regression": ["r1"]},
        "task_digests": {t: ("%064x" % (i + 1)) for i, t in enumerate(["t1", "t2", "r1"])},
        "environment": {"python": "3.10.14", "platform": "linux-x86_64"}})


def run(h, value):
    return {"bundle_hash": h, "scores": {"t%02d" % i: {"pass_rate": value} for i in range(30)}}


def evidence():
    b = bundle()
    h = b["bundle_hash"]
    return {
        "candidate_id": "c1",
        "isolation": {"ok": True, "violations": []},
        "lineage": [{"experiment_id": "exp-20261022-a1b2c3", "candidate_id": "c1",
                     "parent_ids": ["baseline"], "genotype_hash": "%064x" % 5,
                     "created_at": "2026-10-22T10:00:00+00:00", "state": "active"}],
        "bundle": b,
        "security": {"ok": True, "guards": [], "counts": {"pass": 1},
                     "coverage": {"not_frozen": [], "uncovered": []}},
        "canary": {"candidate": run(h, 0.9), "baseline": run(h, 0.9)},
    }


def codes(rep):
    return [r["code"] for r in rep["reasons"]]


def test_complete_passing_evidence_is_allowed():
    rep = G.gate_verdict(evidence())
    assert rep["verdict"] == "allow" and rep["allow"] and rep["reasons"] == []
    assert rep["candidate_id"] == "c1"


@pytest.mark.parametrize("key", G.REQUIRED_KEYS)
def test_every_missing_input_denies(key):
    ev = evidence()
    del ev[key]
    rep = G.gate_verdict(ev)
    assert rep["verdict"] == "deny" and not rep["allow"]
    assert any(r["code"] == "evidence_missing" and r["input"] == key for r in rep["reasons"])


@pytest.mark.parametrize("bad", [None, [], "x", 3])
def test_non_object_evidence_denies_instead_of_raising(bad):
    rep = G.gate_verdict(bad)
    assert rep["verdict"] == "deny" and set(codes(rep)) == {"evidence_missing"}
    assert len(rep["reasons"]) == len(G.REQUIRED_KEYS)


def test_null_input_counts_as_missing():
    ev = evidence()
    ev["security"] = None
    assert "evidence_missing" in codes(G.gate_verdict(ev))


@pytest.mark.parametrize("iso", [{"ok": False, "violations": []},
                                 {"ok": True, "violations": [{"code": "x"}]},
                                 {"ok": True}, {"violations": []}, [], "ok"])
def test_isolation_must_be_ok_and_clean(iso):
    ev = evidence()
    ev["isolation"] = iso
    assert codes(G.gate_verdict(ev)) == ["isolation_failed"]


def test_lineage_violation_denies():
    ev = evidence()
    ev["lineage"][0]["parent_ids"] = ["ghost"]
    assert codes(G.gate_verdict(ev)) == ["lineage_invalid"]


def test_malformed_lineage_denies_not_raises():
    ev = evidence()
    ev["lineage"] = [{"candidate_id": "c1"}]
    assert codes(G.gate_verdict(ev)) == ["lineage_invalid"]
    ev["lineage"] = "nope"
    assert codes(G.gate_verdict(ev)) == ["lineage_invalid"]


def test_lineage_accepts_the_experiments_wrapper():
    ev = evidence()
    ev["lineage"] = {"experiments": ev["lineage"]}
    assert G.gate_verdict(ev)["allow"]


def test_unknown_candidate_denies():
    ev = evidence()
    ev["candidate_id"] = "other"
    assert codes(G.gate_verdict(ev)) == ["candidate_unknown"]


@pytest.mark.parametrize("state", ["promoted", "rejected", "merged"])
def test_only_active_candidates_may_be_promoted(state):
    ev = evidence()
    ev["lineage"][0]["state"] = state
    assert codes(G.gate_verdict(ev)) == ["candidate_not_active"]


def test_bad_candidate_id_type_denies():
    ev = evidence()
    ev["candidate_id"] = 5
    rep = G.gate_verdict(ev)
    assert rep["verdict"] == "deny" and rep["candidate_id"] is None


def test_invalid_bundle_denies():
    ev = evidence()
    ev["bundle"]["pools"]["regression"] = []
    assert "bundle_invalid" in codes(G.gate_verdict(ev))
    ev = evidence()
    ev["bundle"] = [1]
    assert "bundle_invalid" in codes(G.gate_verdict(ev))


def test_bundle_with_wrong_recorded_hash_denies():
    ev = evidence()
    ev["bundle"]["bundle_hash"] = "f" * 64
    assert "bundle_invalid" in codes(G.gate_verdict(ev))


def test_canary_must_name_the_bundle_hash():
    ev = evidence()
    other = "b" * 64
    ev["canary"] = {"candidate": run(other, 0.9), "baseline": run(other, 0.9)}
    assert codes(G.gate_verdict(ev)) == ["bundle_mismatch"]


@pytest.mark.parametrize("sec", [{"ok": False, "coverage": {}},
                                 {"ok": True},
                                 {"ok": True, "coverage": {"not_frozen": ["relay/x.py"]}},
                                 {"ok": True, "coverage": []}, []])
def test_security_must_be_ok_with_coverage(sec):
    ev = evidence()
    ev["security"] = sec
    got = codes(G.gate_verdict(ev))
    assert "security_failed" in got


def test_regressed_canary_denies():
    ev = evidence()
    h = ev["bundle"]["bundle_hash"]
    ev["canary"] = {"candidate": run(h, 0.2), "baseline": run(h, 0.9)}
    rep = G.gate_verdict(ev)
    assert codes(rep) == ["canary_failed"] and "regressed" in rep["reasons"][0]["detail"]


def test_insufficient_canary_denies():
    ev = evidence()
    h = ev["bundle"]["bundle_hash"]
    few = {"bundle_hash": h, "scores": {"t1": {"pass_rate": 1.0}}}
    ev["canary"] = {"candidate": few, "baseline": few}
    rep = G.gate_verdict(ev)
    assert codes(rep) == ["canary_failed"] and "insufficient" in rep["reasons"][0]["detail"]


def test_malformed_canary_denies_not_raises():
    ev = evidence()
    ev["canary"] = {"candidate": 1, "baseline": 2}
    assert codes(G.gate_verdict(ev)) == ["canary_failed"]
    ev["canary"] = {"candidate": ev["canary"]}
    assert codes(G.gate_verdict(ev)) == ["canary_failed"]


def test_canary_cannot_loosen_the_gate():
    ev = evidence()
    h = ev["bundle"]["bundle_hash"]
    ev["canary"] = {"candidate": run(h, 0.5), "baseline": run(h, 0.9),
                    "tolerance": 0.5, "min_samples": 1}
    assert codes(G.gate_verdict(ev)) == ["canary_failed"]


def test_several_failures_are_all_reported_and_input_is_not_mutated():
    ev = evidence()
    ev["isolation"] = {"ok": False, "violations": []}
    ev["security"] = {"ok": False}
    before = copy.deepcopy(ev)
    rep = G.gate_verdict(ev)
    assert {"isolation_failed", "security_failed"} <= set(codes(rep))
    assert ev == before


def test_report_is_stable_json():
    rep = G.gate_verdict(evidence())
    assert json.loads(G.to_json(rep)) == rep and G.to_json(rep) == G.to_json(rep)


def wjson(path, obj):
    path.write_text(json.dumps(obj), encoding="utf-8")
    return str(path)


def cli(*args):
    p = subprocess.run([sys.executable, SCRIPT] + list(args), capture_output=True, timeout=120)
    return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")


def test_cli_allow_deny_and_json(tmp_path):
    good = wjson(tmp_path / "g.json", evidence())
    rc, out, err = cli("--evidence", good)
    assert rc == 0 and "ALLOW" in out, out + err
    ev = evidence()
    del ev["canary"]
    bad = wjson(tmp_path / "b.json", ev)
    rc, out, _ = cli("--evidence", bad)
    assert rc == 1 and "DENY" in out and "evidence_missing" in out
    rc, out, _ = cli("--evidence", bad, "--format", "json")
    assert rc == 1 and json.loads(out)["verdict"] == "deny"


def test_cli_refuses_unreadable_input(tmp_path):
    rc, _, err = cli("--evidence", str(tmp_path / "missing.json"))
    assert rc == 2 and "refused" in err
    junk = tmp_path / "j.json"
    junk.write_text("{not json", encoding="utf-8")
    rc, _, err = cli("--evidence", str(junk))
    assert rc == 2 and "refused" in err


def test_cli_non_object_evidence_is_a_deny(tmp_path):
    rc, out, _ = cli("--evidence", wjson(tmp_path / "l.json", []))
    assert rc == 1 and "DENY" in out


def test_gate_module_does_no_io_and_is_not_imported_by_runtime():
    src = open(os.path.join(REPO, "relay", "org", "lab_promotion_gate.py"), encoding="utf-8").read()
    for word in ("open(", "subprocess", "os.system", "shutil", "socket", "time.", "datetime"):
        assert word not in src.split('"""')[2], word
