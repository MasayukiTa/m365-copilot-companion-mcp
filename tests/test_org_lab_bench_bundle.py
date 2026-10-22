# -*- coding: utf-8 -*-
"""Lab reproducible bench bundle (slice F4)."""
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

SCRIPT = os.path.join(REPO, "scripts", "org_lab_bench_bundle.py")


def spec(**kw):
    s = {"experiment_id": "exp-20261022-a1b2c3",
         "candidate_hash": "%064x" % 0xabc,
         "baseline_hash": "%064x" % 0xdef,
         "evaluator": {"id": "frozen-harness", "version": "3"},
         "model": {"id": "model-x-2026-09-01"},
         "seed": 7,
         "pools": {"evolution": ["t2", "t1"], "regression": ["r1"], "sealed": ["s1"]},
         "task_digests": {t: ("%064x" % (i + 1)) for i, t in enumerate(["t1", "t2", "r1", "s1"])},
         "environment": {"python": "3.10.14", "platform": "linux-x86_64"}}
    s.update(kw)
    return s


def codes(report):
    return [x["code"] for x in report["violations"]]


def test_built_bundle_is_ok_and_hash_is_stable():
    b = B.build_bundle(spec())
    assert B.check_bundle(b)["ok"]
    assert b["bundle_hash"] == B.build_bundle(spec())["bundle_hash"]
    assert b["bundle_hash"] == B.compute_hash(spec())


def test_task_order_does_not_change_the_hash():
    s2 = spec(pools={"evolution": ["t1", "t2"], "regression": ["r1"], "sealed": ["s1"]})
    assert B.compute_hash(spec()) == B.compute_hash(s2)


def test_any_measurement_change_changes_the_hash():
    base = B.compute_hash(spec())
    assert B.compute_hash(spec(seed=8)) != base
    s = spec()
    s["task_digests"]["t1"] = "%064x" % 99
    assert B.compute_hash(s) != base


def test_tampered_bundle_is_reported():
    b = B.build_bundle(spec())
    b["seed"] = 8
    assert codes(B.check_bundle(b)) == ["bundle_hash_mismatch"]


def test_missing_or_short_hash_is_reported():
    assert codes(B.check_bundle(spec())) == ["bundle_hash_format"]


@pytest.mark.parametrize("model", ["", "latest", "Default", "gpt:latest"])
def test_unpinned_model(model):
    b = B.build_bundle(spec(model={"id": model}))
    assert codes(B.check_bundle(b)) == ["unpinned_model"]


def test_seed_must_be_an_integer_not_bool_or_none():
    for seed in (None, True, "7", 1.5):
        b = B.build_bundle(spec(seed=seed))
        assert codes(B.check_bundle(b)) == ["seed_invalid"], seed


def test_evaluator_and_environment_required():
    b = B.build_bundle(spec(evaluator={"id": "h", "version": ""}, environment={"python": "3"}))
    assert sorted(codes(B.check_bundle(b))) == ["environment_incomplete", "evaluator_unversioned"]


def test_id_and_hash_shapes():
    b = B.build_bundle(spec(experiment_id="exp-1", candidate_hash="ABC", baseline_hash=""))
    assert sorted(codes(B.check_bundle(b))) == [
        "baseline_hash_format", "candidate_hash_format", "experiment_id_format"]


def test_pool_rules():
    s = spec(pools={"evolution": ["t1", "t1"], "regression": [], "extra": ["t2"], "sealed": ["t1"]})
    got = codes(B.check_bundle(B.build_bundle(s)))
    for c in ("duplicate_task", "empty_pool", "unknown_pool", "pool_overlap"):
        assert c in got, c
    s = spec(pools={"evolution": ["t1", "t2"]})
    assert "missing_pool" in codes(B.check_bundle(B.build_bundle(s)))


def test_task_digest_rules():
    s = spec()
    del s["task_digests"]["t1"]
    s["task_digests"]["ghost"] = "%064x" % 5
    s["task_digests"]["t2"] = "short"
    got = B.check_bundle(B.build_bundle(s))
    assert sorted(codes(got)) == ["task_digest_extra", "task_digest_format", "task_digest_missing"]
    assert {x["id"] for x in got["violations"]} == {"t1", "ghost", "t2"}


def test_bad_input_is_refused_not_reported():
    for bad in ([], "x", {"pools": []}, {"pools": {"evolution": "t1"}},
                {"task_digests": {"t1": 3}}, {"evaluator": []}):
        with pytest.raises(B.LabBundleError):
            B.check_bundle(bad)


def test_compare_same_setup_different_candidate_is_comparable():
    a = B.build_bundle(spec())
    b = B.build_bundle(spec(candidate_hash="%064x" % 0x123, experiment_id="exp-20261022-ffffff"))
    r = B.compare_bundles(a, b)
    assert r["comparable"] and r["differences"] == [] and r["invalid"] == []


def test_compare_lists_every_differing_measurement_field():
    a = B.build_bundle(spec())
    s = spec(seed=9, model={"id": "model-y"})
    s["pools"] = {"evolution": ["t1"], "regression": ["r1"]}
    r = B.compare_bundles(a, B.build_bundle(s))
    assert not r["comparable"]
    assert r["differences"] == ["model", "seed", "pools"]


def test_compare_refuses_an_invalid_bundle():
    a = B.build_bundle(spec())
    bad = copy.deepcopy(a)
    bad["bundle_hash"] = "0" * 64
    r = B.compare_bundles(a, bad)
    assert not r["comparable"] and r["invalid"] == ["b"] and r["differences"] == []


def test_is_pure_and_default_off():
    src = open(os.path.join(REPO, "relay", "org", "lab_bench_bundle.py"), encoding="utf-8").read()
    for token in ("open(", "subprocess", "os.environ", "time.time", "datetime", "socket"):
        assert token not in src.split('"""', 2)[2], token


# ---- CLI (the caller that makes the module reachable) ----

def run(*args):
    p = subprocess.run([sys.executable, SCRIPT] + list(args), capture_output=True)
    return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")


def write(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    return str(p)


def test_cli_ok_and_hash(tmp_path):
    f = write(tmp_path, "b.json", B.build_bundle(spec()))
    code, out, _ = run("--check", "--bundle", f)
    assert code == 0 and "lab bench bundle: ok" in out
    code, out, _ = run("--hash", "--bundle", write(tmp_path, "s.json", spec()))
    assert code == 0 and out.strip() == B.compute_hash(spec())


def test_cli_violation_and_compare(tmp_path):
    a = write(tmp_path, "a.json", B.build_bundle(spec()))
    b = write(tmp_path, "b.json", B.build_bundle(spec(seed=2)))
    code, out, _ = run("--check", "--bundle", a, "--compare", b)
    assert code == 1 and "comparable: NO" in out and "differs in: seed" in out
    code, out, _ = run("--check", "--bundle", write(tmp_path, "s.json", spec()), "--format", "json")
    assert code == 1 and json.loads(out)["violations"][0]["code"] == "bundle_hash_format"


def test_cli_refuses_bad_input(tmp_path):
    code, _, err = run("--check", "--bundle", write(tmp_path, "x.json", ["nope"]))
    assert code == 2 and "refused" in err
    code, _, err = run("--check", "--bundle", str(tmp_path / "missing.json"))
    assert code == 2
