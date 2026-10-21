# -*- coding: utf-8 -*-
"""Lab strict lineage check (slice F3)."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.org import lab_lineage as L  # noqa: E402

SCRIPT = os.path.join(REPO, "scripts", "org_lab_lineage.py")


def rec(n, parents=("baseline",), minute=0, **kw):
    r = {"experiment_id": "exp-20261022-%06x" % (n + 0xa0000),
         "candidate_id": "c%d" % n,
         "parent_ids": list(parents),
         "genotype_hash": ("%064x" % (n + 0xabc)),
         "created_at": "2026-10-22T10:%02d:00Z" % minute,
         "state": "active"}
    r.update(kw)
    return r


def codes(report):
    return [x["code"] for x in report["violations"]]


def tree():
    return [rec(1), rec(2, ["c1"], 1), rec(3, ["c1"], 2), rec(4, ["c2", "c3"], 3)]


def test_valid_tree_is_ok():
    rep = L.check_lineage(tree())
    assert rep["ok"] and rep["violations"] == [] and rep["count"] == 4


def test_duplicate_experiment_id():
    recs = tree()
    recs[1]["experiment_id"] = recs[0]["experiment_id"]
    assert codes(L.check_lineage(recs)) == ["duplicate_experiment_id"]


def test_duplicate_candidate_id():
    recs = tree()
    recs[2]["candidate_id"] = "c2"
    assert "duplicate_candidate_id" in codes(L.check_lineage(recs))


def test_missing_parent_and_root_marker_is_fine():
    recs = [rec(1), rec(2, ["ghost"], 1)]
    rep = L.check_lineage(recs)
    assert codes(rep) == ["missing_parent"] and rep["violations"][0]["id"] == "c2"


def test_self_parent_is_not_also_a_cycle():
    rep = L.check_lineage([rec(1, ["c1"])])
    assert codes(rep) == ["self_parent"]


def test_cycle_reported_once_on_smallest_id():
    recs = [rec(1, ["c3"]), rec(2, ["c1"], 1), rec(3, ["c2"], 2)]
    rep = L.check_lineage(recs)
    cyc = [x for x in rep["violations"] if x["code"] == "cycle"]
    assert len(cyc) == 1 and cyc[0]["id"] == "c1"


@pytest.mark.parametrize("bad", ["exp-2026102-a1b2c3", "exp-20261022-a1b2c", "exp-20261022-A1B2C3",
                                 "EXP-20261022-a1b2c3", "x"])
def test_experiment_id_format(bad):
    assert codes(L.check_lineage([rec(1, experiment_id=bad)])) == ["experiment_id_format"]


def test_experiment_id_longer_hex_is_accepted():
    assert L.check_lineage([rec(1, experiment_id="exp-20261022-0123456789abcdef")])["ok"]


@pytest.mark.parametrize("bad", ["", "abc", "A" * 64, "g" * 64, "a" * 63, "a" * 65])
def test_genotype_hash_format(bad):
    assert codes(L.check_lineage([rec(1, genotype_hash=bad)])) == ["genotype_hash_format"]


@pytest.mark.parametrize("val", ["", None])
def test_orphan_experiment(val):
    assert codes(L.check_lineage([rec(1, experiment_id=val)])) == ["orphan_experiment"]


def test_timestamp_order_child_before_parent():
    recs = [rec(1, minute=5), rec(2, ["c1"], 1)]
    rep = L.check_lineage(recs)
    assert codes(rep) == ["timestamp_order"] and rep["violations"][0]["id"] == "c2"


def test_equal_timestamps_are_allowed():
    assert L.check_lineage([rec(1, minute=3), rec(2, ["c1"], 3)])["ok"]


def test_lineage_of_returns_root_first_chain():
    assert L.lineage_of(tree(), "c4") == ["c1", "c2", "c3", "c4"]
    assert L.lineage_of(tree(), "c1") == ["c1"]


def test_lineage_of_unknown_candidate_refused():
    with pytest.raises(L.LabLineageError):
        L.lineage_of(tree(), "nope")


def test_lineage_of_terminates_on_cycle():
    recs = [rec(1, ["c2"]), rec(2, ["c1"], 1)]
    assert sorted(L.lineage_of(recs, "c2")) == ["c1", "c2"]


def test_summary_depth_roots_branching():
    s = L.summarize(tree())
    assert (s["depth"], s["roots"], s["max_branching"]) == (3, 1, 2)
    assert s["mean_branching"] == 1.333  # c1 has 2 children, c2 and c3 have 1 each


def test_summary_empty_and_is_deterministic():
    assert L.summarize([])["depth"] == 0
    shuffled = list(reversed(tree()))
    assert L.summarize(shuffled) == L.summarize(tree())
    assert L.check_lineage(shuffled) == L.check_lineage(tree())


def test_bad_input_is_refused():
    with pytest.raises(L.LabLineageError):
        L.check_lineage("nope")
    with pytest.raises(L.LabLineageError):
        L.check_lineage([rec(1, created_at="yesterday")])
    with pytest.raises(L.LabLineageError):
        L.check_lineage([rec(1, state="weird")])
    with pytest.raises(L.LabLineageError):
        L.check_lineage([rec(1, parent_ids="c0")])


def test_input_is_not_mutated():
    recs = tree()
    before = json.dumps(recs, sort_keys=True)
    L.check_lineage(recs)
    L.summarize(recs)
    L.lineage_of(recs, "c4")
    assert json.dumps(recs, sort_keys=True) == before


def run_cli(tmp_path, data, *extra):
    f = tmp_path / "r.json"
    f.write_text(json.dumps(data), encoding="utf-8")
    return subprocess.run([sys.executable, SCRIPT, "--check", "--records", str(f), *extra],
                          capture_output=True, cwd=REPO)


def test_cli_ok_and_violation_exit_codes(tmp_path):
    ok = run_cli(tmp_path, tree())
    assert ok.returncode == 0 and b"lab lineage: ok" in ok.stdout
    bad = run_cli(tmp_path, {"experiments": [rec(1, ["ghost"])]}, "--format", "json")
    assert bad.returncode == 1
    out = json.loads(bad.stdout.decode("utf-8"))
    assert out["violations"][0]["code"] == "missing_parent" and out["summary"]["roots"] == 1


def test_cli_refuses_bad_input(tmp_path):
    p = run_cli(tmp_path, [rec(1, created_at="x")])
    assert p.returncode == 2 and b"refused" in p.stderr
