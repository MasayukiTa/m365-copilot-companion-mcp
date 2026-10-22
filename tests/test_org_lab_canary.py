# -*- coding: utf-8 -*-
"""Lab canary / shadow evaluator (slice F6)."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.org import lab_canary as C  # noqa: E402

SCRIPT = os.path.join(REPO, "scripts", "org_lab_canary.py")
H = "a" * 64
SEC_OK = {"ok": True}


def run(values, bundle=H, metric="pass_rate"):
    return {"bundle_hash": bundle,
            "scores": {"t%02d" % i: {metric: v} for i, v in enumerate(values)}}


def codes(rep):
    return [p["code"] for p in rep["problems"]]


def test_equal_runs_pass():
    rep = C.shadow_verdict(run([1.0, 0.0] * 20), run([1.0, 0.0] * 20), SEC_OK)
    assert rep["verdict"] == "pass" and rep["ok"] and rep["samples"] == 40
    assert rep["metrics"]["pass_rate"]["delta"] == 0


def test_small_drop_within_tolerance_passes_and_boundary_is_inclusive():
    base = run([0.82] * 30)
    assert C.shadow_verdict(run([0.80] * 30), base, SEC_OK)["verdict"] == "pass"
    assert C.shadow_verdict(run([0.79] * 30), base, SEC_OK)["verdict"] == "fail"


def test_regression_beyond_tolerance_fails_and_improvement_passes():
    rep = C.shadow_verdict(run([0.5] * 30), run([0.9] * 30), SEC_OK)
    assert rep["verdict"] == "fail" and codes(rep) == ["regressed"]
    assert rep["metrics"]["pass_rate"]["regressed"] is True
    assert C.shadow_verdict(run([0.95] * 30), run([0.9] * 30), SEC_OK)["verdict"] == "pass"


def test_custom_tolerance():
    base, cand = run([0.9] * 30), run([0.8] * 30)
    assert C.shadow_verdict(cand, base, SEC_OK)["verdict"] == "fail"
    assert C.shadow_verdict(cand, base, SEC_OK, tolerance=0.1)["verdict"] == "pass"


def test_too_few_samples_is_insufficient_not_pass():
    rep = C.shadow_verdict(run([1.0] * 5), run([1.0] * 5), SEC_OK)
    assert rep["verdict"] == "insufficient" and not rep["ok"] and codes(rep) == ["too_few_samples"]
    assert C.shadow_verdict(run([1.0] * 5), run([1.0] * 5), SEC_OK, min_samples=5)["ok"]


def test_a_failure_outranks_insufficient():
    rep = C.shadow_verdict(run([0.0] * 5), run([1.0] * 5), SEC_OK)
    assert rep["verdict"] == "fail" and set(codes(rep)) == {"regressed", "too_few_samples"}


@pytest.mark.parametrize("sec", [None, {}, {"ok": False}, {"ok": "yes"}, [], 1])
def test_security_report_must_say_ok(sec):
    rep = C.shadow_verdict(run([1.0] * 30), run([1.0] * 30), sec)
    assert rep["verdict"] == "fail" and codes(rep) == ["security_not_ok"]


def test_different_bundles_are_not_compared():
    rep = C.shadow_verdict(run([1.0] * 30, bundle="b" * 64), run([1.0] * 30), SEC_OK)
    assert "bundle_mismatch" in codes(rep) and rep["verdict"] == "fail"


def test_candidate_cannot_pass_by_dropping_a_metric():
    base = {"bundle_hash": H, "scores": {"t%d" % i: {"a": 0.9, "b": 0.9} for i in range(30)}}
    cand = {"bundle_hash": H, "scores": {"t%d" % i: {"a": 0.9} for i in range(30)}}
    rep = C.shadow_verdict(cand, base, SEC_OK)
    assert rep["verdict"] == "fail" and "metrics_mismatch" in codes(rep)


def test_no_common_metric_fails():
    rep = C.shadow_verdict(run([1.0] * 30, metric="x"), run([1.0] * 30, metric="y"), SEC_OK)
    assert "metrics_mismatch" in codes(rep) and not rep["ok"]


def test_tasks_on_one_side_only_are_listed_and_not_scored():
    base = run([0.9] * 30)
    cand = run([0.9] * 30)
    cand["scores"]["extra"] = {"pass_rate": 0.0}
    base["scores"]["gone"] = {"pass_rate": 1.0}
    rep = C.shadow_verdict(cand, base, SEC_OK)
    assert rep["only_candidate"] == ["extra"] and rep["only_baseline"] == ["gone"]
    assert rep["samples"] == 30 and rep["verdict"] == "pass"


def test_multiple_metrics_each_checked():
    base = {"bundle_hash": H, "scores": {"t%d" % i: {"a": 0.9, "b": 0.9} for i in range(30)}}
    cand = {"bundle_hash": H, "scores": {"t%d" % i: {"a": 0.95, "b": 0.5} for i in range(30)}}
    rep = C.shadow_verdict(cand, base, SEC_OK)
    assert rep["metrics"]["a"]["regressed"] is False and rep["metrics"]["b"]["regressed"] is True


@pytest.mark.parametrize("bad", [None, [], {"bundle_hash": "x", "scores": {}},
                                 {"bundle_hash": H}, {"bundle_hash": H, "scores": {"t": 1}},
                                 {"bundle_hash": H, "scores": {"t": {"m": True}}},
                                 {"bundle_hash": H, "scores": {"t": {"m": 1.5}}},
                                 {"bundle_hash": H, "scores": {"t": {"m": float("nan")}}}])
def test_bad_runs_raise(bad):
    with pytest.raises(C.LabCanaryError):
        C.shadow_verdict(bad, run([1.0]), SEC_OK)
    with pytest.raises(C.LabCanaryError):
        C.shadow_verdict(run([1.0]), bad, SEC_OK)


@pytest.mark.parametrize("tol", [-0.1, 0.6, True, "0.1", float("nan")])
def test_bad_tolerance_raises(tol):
    with pytest.raises(C.LabCanaryError):
        C.shadow_verdict(run([1.0]), run([1.0]), SEC_OK, tolerance=tol)


@pytest.mark.parametrize("n", [0, -1, True, 1.5])
def test_bad_min_samples_raises(n):
    with pytest.raises(C.LabCanaryError):
        C.shadow_verdict(run([1.0]), run([1.0]), SEC_OK, min_samples=n)


def test_report_is_stable_json():
    a = C.shadow_verdict(run([0.5] * 30), run([0.9] * 30), SEC_OK)
    b = C.shadow_verdict(run([0.5] * 30), run([0.9] * 30), SEC_OK)
    assert C.to_json(a) == C.to_json(b)


def test_module_is_pure_no_io_imports():
    with open(C.__file__, encoding="utf-8") as fh:
        src = fh.read()
    for word in ("import subprocess", "import os", "open(", "import socket", "import time"):
        assert word not in src


# ---- CLI ----

def wjson(path, obj):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh)
    return str(path)


def cli(*args):
    p = subprocess.run([sys.executable, SCRIPT] + list(args), capture_output=True, timeout=120)
    return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")


def test_cli_pass_and_fail_and_json(tmp_path):
    base = wjson(tmp_path / "b.json", run([0.9] * 30))
    good = wjson(tmp_path / "g.json", run([0.9] * 30))
    bad = wjson(tmp_path / "x.json", run([0.4] * 30))
    sec = wjson(tmp_path / "s.json", {"ok": True})
    rc, out, err = cli("--candidate", good, "--baseline", base, "--security", sec)
    assert rc == 0 and "PASS" in out, out + err
    rc, out, _ = cli("--candidate", bad, "--baseline", base, "--security", sec)
    assert rc == 1 and "REGRESSED" in out and "regressed" in out
    rc, out, _ = cli("--candidate", good, "--baseline", base, "--security", sec,
                     "--format", "json")
    assert rc == 0 and json.loads(out)["verdict"] == "pass"


def test_cli_insufficient_and_bad_security(tmp_path):
    base = wjson(tmp_path / "b.json", run([0.9] * 3))
    sec = wjson(tmp_path / "s.json", {"ok": True})
    rc, out, _ = cli("--candidate", base, "--baseline", base, "--security", sec)
    assert rc == 1 and "INSUFFICIENT" in out
    rc, out, _ = cli("--candidate", base, "--baseline", base, "--security", sec,
                     "--min-samples", "3")
    assert rc == 0
    badsec = wjson(tmp_path / "bs.json", {"ok": False})
    rc, out, _ = cli("--candidate", base, "--baseline", base, "--security", badsec,
                     "--min-samples", "3")
    assert rc == 1 and "security_not_ok" in out


def test_cli_refuses_bad_input(tmp_path):
    base = wjson(tmp_path / "b.json", run([0.9] * 3))
    rc, _, err = cli("--candidate", base, "--baseline", base,
                     "--security", str(tmp_path / "missing.json"))
    assert rc == 2 and "refused" in err
    sec = wjson(tmp_path / "s.json", {"ok": True})
    rc, _, err = cli("--candidate", base, "--baseline", base, "--security", sec,
                     "--tolerance", "0.9")
    assert rc == 2 and "refused" in err
