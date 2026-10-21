# -*- coding: utf-8 -*-
"""Multi-objective archive for genotype results (slice E4): Pareto front, pure, order independent."""
from __future__ import annotations

import copy
import itertools
import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.org import genotype_archive as A  # noqa: E402

SCRIPT = os.path.join(REPO, "scripts", "org_genotype_archive.py")


def E(i, **axes):
    return {"id": i, "axes": axes}


def ids(archive):
    return [e["id"] for e in archive["entries"]]


def test_dominance_direction_per_axis():
    assert A.dominates(E("a", success=1.0, wall_time=5), E("b", success=0.5, wall_time=5))
    assert A.dominates(E("a", success=1.0, wall_time=4), E("b", success=1.0, wall_time=5))
    assert not A.dominates(E("a", success=1.0, wall_time=9), E("b", success=0.5, wall_time=5))
    assert not A.dominates(E("b", success=0.5, wall_time=5), E("a", success=1.0, wall_time=9))


def test_dominance_with_ties():
    a, b = E("a", success=1.0, turns=3), E("b", success=1.0, turns=3)
    assert not A.dominates(a, b) and not A.dominates(b, a)
    assert not A.dominates(a, a)
    assert sorted(ids(A.build([a, b]))) == ["a", "b"]  # equal axes, different ids: both stay
    assert ids(A.build([a, a])) == ["a"]  # same id and axes: kept once


def test_empty_archive():
    assert A.empty()["entries"] == []
    assert A.pareto_front([]) == []
    assert A.build([])["entries"] == []
    assert ids(A.insert(A.empty(), E("a", success=1.0))) == ["a"]


def test_missing_axis_counts_as_worst():
    measured = E("m", success=0.5, turns=10)
    unmeasured = E("u", success=0.5)  # no turns
    assert A.dominates(measured, unmeasured)
    assert ids(A.build([unmeasured, measured])) == ["m"]
    # nothing known at all can never beat anything, and junk values count as missing
    bare = E("x", success=True, turns=float("nan"), wall_time="slow", bogus=1)
    assert A.make_entry("x", bare["axes"])["axes"] == {}
    assert not A.dominates(bare, measured)
    assert A.dominates(measured, bare)


def test_front_keeps_tradeoffs_and_drops_dominated():
    fast = E("fast", success=0.6, wall_time=1)
    good = E("good", success=0.9, wall_time=9)
    bad = E("bad", success=0.5, wall_time=10)
    assert sorted(ids(A.build([fast, good, bad]))) == ["fast", "good"]


def test_front_is_stable_and_idempotent():
    es = [E("a", success=0.6, wall_time=1), E("b", success=0.9, wall_time=9),
          E("c", success=0.5, wall_time=10), E("d", success=0.9, wall_time=9, turns=2)]
    front = A.build(es)
    assert A.build(front["entries"]) == front
    assert A.to_json(A.build(front["entries"])) == A.to_json(front)


def test_insertion_order_independence():
    es = [E("a", success=0.6, wall_time=1), E("b", success=0.9, wall_time=9),
          E("c", success=0.5, wall_time=10), E("d", success=0.9, wall_time=9),
          E("e", success=0.7, retries=1)]
    want = A.to_json(A.build(es))
    for perm in itertools.permutations(es):
        arc = A.empty()
        for e in perm:
            arc = A.insert(arc, e)
        assert A.to_json(arc) == want


def test_insert_never_mutates():
    arc = A.insert(A.empty(), E("a", success=0.5))
    before = copy.deepcopy(arc)
    entry = E("b", success=0.9)
    entry_before = copy.deepcopy(entry)
    new = A.insert(arc, entry)
    assert arc == before and entry == entry_before
    assert new is not arc and ids(new) == ["b"]


def test_json_roundtrip_and_bad_input():
    arc = A.build([E("a", success=0.6, wall_time=1), E("b", success=0.9, wall_time=9)])
    assert A.from_json(A.to_json(arc)) == arc
    with pytest.raises(A.ArchiveError):
        A.from_json("not json")
    with pytest.raises(A.ArchiveError):
        A.from_json("{}")
    with pytest.raises(A.ArchiveError):
        A.make_entry("", {})


def _report():
    return {"parent": "p", "runs": [
        {"genotype": "p", "pool": "evolution", "status": "ok", "score": 1.0},
        {"genotype": "p", "pool": "regression", "status": "ok", "score": 1.0},
        {"genotype": "c1", "pool": "evolution", "status": "ok", "score": 2.0},
        {"genotype": "c1", "pool": "regression", "status": "error", "score": None},
        {"genotype": "c2", "pool": "evolution", "status": "error", "score": None}],
        "candidates": [{"id": "c1", "axes": {"wall_time": 3.0}}, {"id": "c2"}]}


def test_entries_from_report_derives_success_and_infra_failures():
    by = {e["id"]: e["axes"] for e in A.entries_from_report(_report())}
    assert by["p"] == {"success": 1.0, "infra_failures": 0.0}
    assert by["c1"] == {"success": 2.0, "infra_failures": 1.0, "wall_time": 3.0}
    assert by["c2"] == {"infra_failures": 1.0}
    with pytest.raises(A.ArchiveError):
        A.entries_from_report({})


def test_cli_prints_front_as_text_and_json_and_writes_nothing(tmp_path):
    src = tmp_path / "r.json"
    src.write_text(json.dumps(_report()), encoding="utf-8")
    before = sorted(os.listdir(tmp_path))
    txt = subprocess.run([sys.executable, SCRIPT, str(src)], capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert txt.returncode == 0 and "entries on the front" in txt.stdout
    js = subprocess.run([sys.executable, SCRIPT, str(src), "--format", "json"],
                        capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert js.returncode == 0
    assert {e["id"] for e in json.loads(js.stdout)["entries"]} == {"p", "c1"}
    assert sorted(os.listdir(tmp_path)) == before
    arc = tmp_path / "a.json"
    arc.write_text(js.stdout, encoding="utf-8")
    again = subprocess.run([sys.executable, SCRIPT, str(arc), "--format", "json"],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert again.returncode == 0 and json.loads(again.stdout) == json.loads(js.stdout)
    bad = subprocess.run([sys.executable, SCRIPT, str(tmp_path / "missing.json")],
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert bad.returncode == 2
