# -*- coding: utf-8 -*-
"""Failure taxonomy and replay curriculum (slice G1). Synthetic data only."""
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

from relay.org import failure_taxonomy as T  # noqa: E402

SCRIPT = os.path.join(REPO, "scripts", "org_failure_taxonomy.py")


def rec(eid, ts=None, label=None, task="t", **outcome):
    r = {"episode_id": eid, "task_class": task, "failure_class": label, "outcome": outcome}
    if ts is not None:
        r["ts"] = ts
    return r


@pytest.mark.parametrize("record,expected", [
    (rec("a", security_success=False, functional_success=False), T.SECURITY),
    ({"episode_id": "a", "security_events": [{"x": 1}], "outcome": {"functional_success": True}}, T.SECURITY),
    ({"episode_id": "a", "security_events_count": 2, "outcome": {}}, T.SECURITY),
    (rec("a", timed_out=True, functional_success=False), T.TIMEOUT),
    ({"episode_id": "a", "verification": {"checks_failed": 1}, "outcome": {"functional_success": False}}, T.VERIFICATION),
    (rec("a", verification_success=False), T.VERIFICATION),
    (rec("a", functional_success=False), T.FUNCTIONAL),
    (rec("a", label="something"), T.UNEXPLAINED),
    (rec("a", functional_success=True), T.SUCCESS),
    (rec("a"), T.NO_OUTCOME),
    ("not a dict", T.MALFORMED),
])
def test_rules(record, expected):
    assert T.classify_record(record)[0] == expected


def test_zero_checks_failed_and_bool_are_not_failures():
    r = {"episode_id": "a", "verification": {"checks_failed": 0}, "outcome": {"functional_success": True}}
    assert T.classify_record(r)[0] == T.SUCCESS
    r["verification"]["checks_failed"] = True
    assert T.classify_record(r)[0] == T.SUCCESS


def test_label_with_signal_uses_the_signal():
    assert T.classify_record(rec("a", label="x", functional_success=False))[0] == T.FUNCTIONAL


def records():
    return [rec("f1", ts=10, functional_success=False),
            rec("f2", ts=30, functional_success=False),
            rec("s1", ts=20, security_success=False),
            rec("s2", ts=5, security_success=False),
            rec("v1", ts=99, verification_success=False),
            rec("ok", ts=50, functional_success=True),
            rec("none"),
            "junk"]


def test_counts_and_curriculum_order():
    rep = T.build_report(records())
    assert (rep["total"], rep["failures"], rep["successes"], rep["no_outcome"], rep["malformed"]) == (8, 5, 1, 1, 1)
    # tie at count 2 broken by the more recent latest ts: functional (30) before security (20)
    assert [c["class"] for c in rep["curriculum"]] == [T.FUNCTIONAL, T.SECURITY, T.VERIFICATION]
    assert rep["curriculum"][0]["episode_ids"] == ["f2", "f1"]
    assert [c["rank"] for c in rep["curriculum"]] == [1, 2, 3]
    assert rep["skipped"] == [{"index": 7, "code": "malformed"}]


def test_frequency_beats_recency():
    rep = T.build_report([rec("a", ts=1, security_success=False), rec("b", ts=2, security_success=False),
                          rec("c", ts=100, functional_success=False)])
    assert rep["curriculum"][0]["class"] == T.SECURITY


def test_unknown_ts_sorts_after_known_at_equal_count():
    rep = T.build_report([rec("a", functional_success=False), rec("b", ts=1, timed_out=True)])
    assert [c["class"] for c in rep["curriculum"]] == [T.TIMEOUT, T.FUNCTIONAL]
    assert rep["classes"][T.FUNCTIONAL]["without_ts"] == 1
    assert rep["classes"][T.FUNCTIONAL]["latest_ts"] is None


def test_bool_ts_is_not_a_time():
    rep = T.build_report([rec("a", ts=True, functional_success=False)])
    assert rep["classes"][T.FUNCTIONAL]["latest_ts"] is None


def test_labels_and_task_classes_are_tallied():
    rep = T.build_report([rec("a", label="x", task="p", functional_success=False),
                          rec("b", task="p", functional_success=False)])
    c = rep["classes"][T.FUNCTIONAL]
    assert c["labels"] == {"x": 1, "(none)": 1} and c["task_classes"] == {"p": 2}


def test_empty_and_non_list_input():
    for bad in ([], None, {"records": []}):
        rep = T.build_report(bad)
        assert rep["total"] == 0 and rep["curriculum"] == []


def test_pure_does_not_mutate_input_and_is_deterministic():
    before = records()
    snapshot = copy.deepcopy(before)
    a = T.to_json(T.build_report(before))
    assert before == snapshot
    assert a == T.to_json(T.build_report(copy.deepcopy(before)))
    json.loads(a)


def test_module_has_no_io_or_clock_imports():
    src = open(os.path.join(REPO, "relay", "org", "failure_taxonomy.py"), encoding="utf-8").read()
    for word in ("import os", "import time", "import socket", "subprocess", "urllib", "requests", "open("):
        assert word not in src.split('"""', 2)[2]


def test_no_runtime_module_imports_it():
    hits = []
    for base in ("relay", "tools", "app", "bridge"):
        root = os.path.join(REPO, base)
        for d, _, files in os.walk(root):
            for f in files:
                if f.endswith(".py") and f != "failure_taxonomy.py":
                    p = os.path.join(d, f)
                    try:
                        if "failure_taxonomy" in open(p, encoding="utf-8", errors="replace").read():
                            hits.append(p)
                    except OSError:
                        pass
    assert hits == []


def run_cli(tmp_path, data, *extra):
    p = tmp_path / "r.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return subprocess.run([sys.executable, "-I", SCRIPT, "--records", str(p), *extra],
                          capture_output=True, timeout=60)


def test_cli_text_and_json(tmp_path):
    before = sorted(os.listdir(tmp_path))
    r = run_cli(tmp_path, records())
    assert r.returncode == 0
    out = r.stdout.decode("utf-8")
    assert "replay 1: functional_failure x2" in out and "episode f2" in out
    j = run_cli(tmp_path, {"records": records()}, "--format", "json")
    assert j.returncode == 0 and json.loads(j.stdout.decode("utf-8"))["failures"] == 5
    assert sorted(os.listdir(tmp_path)) == sorted(before + ["r.json"])


def test_cli_refuses_bad_input(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    r = subprocess.run([sys.executable, "-I", SCRIPT, "--records", str(p)], capture_output=True, timeout=60)
    assert r.returncode == 2
    assert run_cli(tmp_path, {"records": "x"}).returncode == 2
    r = subprocess.run([sys.executable, "-I", SCRIPT, "--records", str(tmp_path / "missing.json")],
                       capture_output=True, timeout=60)
    assert r.returncode == 2
