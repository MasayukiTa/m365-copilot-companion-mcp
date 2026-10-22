# -*- coding: utf-8 -*-
"""Population novelty and quality-diversity report (slice G4). Synthetic data only."""
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

from relay.org import novelty as N  # noqa: E402

SCRIPT = os.path.join(REPO, "scripts", "org_novelty.py")


def mem(mid, text="", **extra):
    r = {"genotype_id": mid, "parts": {"role": text}}
    r.update(extra)
    return r


def test_empty_and_non_list_input():
    for bad in ([], None, "x", {"a": 1}):
        r = N.build_report(bad)
        assert r["total"] == 0 and r["members"] == 0 and r["malformed"] == 0
        assert r["duplicate_clusters"] == [] and r["novelty"] == {}
        assert r["quality_diversity"]["qd_score"] == 0


@pytest.mark.parametrize("record,expected", [
    ({"genotype_id": "a"}, "a"),
    ({"agent_id": "b"}, "b"),
    ({"id": "c"}, "c"),
    ({"genotype_id": " ", "id": "c"}, "c"),
    ({"id": 5}, None),
    ({}, None),
    ([], None),
])
def test_member_id_of(record, expected):
    assert N.member_id_of(record) == expected


def test_malformed_and_duplicate_ids_skipped_and_counted():
    rows = [mem("a", "x"), "junk", None, {"parts": {}}, mem("a", "y"), mem("b", "z")]
    r = N.build_report(rows)
    assert r["total"] == 6 and r["malformed"] == 4 and r["members"] == 2
    assert {"index": 4, "code": "duplicate_id", "id": "a"} in r["skipped"]
    assert r["novelty"]["a"]["record_index"] == 0


def test_structural_distance_basics():
    a = frozenset({1, 2})
    assert N.structural_distance(a, a) == 0.0
    assert N.structural_distance(a, frozenset({3})) == 1.0
    assert N.structural_distance(a, frozenset({2, 3})) == pytest.approx(1 - 1 / 3)
    assert N.structural_distance(frozenset(), frozenset()) is None


def test_features_from_genes_and_parts():
    f = N.features_of({"genes": {"depth": 2, "bad": [1]}, "parts": {"r": "a b a", "t": 5}})
    assert ("g", "depth", "2") in f and ("p", "r", "a") in f and ("p", "r", "b") in f
    assert not any(x[1] == "bad" or x[1] == "t" for x in f)


def test_gene_value_type_matters():
    f1 = N.features_of({"genes": {"x": 1}})
    f2 = N.features_of({"genes": {"x": "1"}})
    assert f1 != f2


def test_identical_members_have_zero_novelty_and_cluster():
    r = N.build_report([mem("a", "x y"), mem("b", "x y"), mem("c", "p q")])
    assert r["novelty"]["a"]["structural_novelty"] == pytest.approx(0.5)
    assert r["duplicate_clusters"] == [{"ids": ["a", "b"], "size": 2,
                                        "max_pairwise_distance": 0.0}]
    assert r["members_in_duplicate_clusters"] == 2


def test_most_novel_member_has_highest_novelty():
    rows = [mem("a", "x y z"), mem("b", "x y z w"), mem("c", "x y z v"), mem("d", "q r s t")]
    nov = N.build_report(rows, k=2)["novelty"]
    assert max(nov, key=lambda i: nov[i]["structural_novelty"]) == "d"


def test_k_limits_neighbours():
    rows = [mem("a", "x"), mem("b", "x y"), mem("c", "q r s t u v")]
    k1 = N.build_report(rows, k=1)["novelty"]["a"]["structural_novelty"]
    k2 = N.build_report(rows, k=2)["novelty"]["a"]["structural_novelty"]
    assert k1 == pytest.approx(0.5) and k2 > k1


def test_k_larger_than_population_uses_all_available():
    r = N.build_report([mem("a", "x"), mem("b", "y")], k=50)
    assert r["novelty"]["a"]["structural_novelty"] == 1.0


def test_single_member_has_no_novelty_not_zero():
    r = N.build_report([mem("a", "x")])
    assert r["novelty"]["a"]["structural_novelty"] is None
    assert r["novelty"]["a"]["behavioral_novelty"] is None


def test_member_without_features_has_no_structural_novelty():
    r = N.build_report([{"genotype_id": "a"}, {"genotype_id": "b"}])
    assert r["novelty"]["a"]["structural_novelty"] is None
    assert r["duplicate_clusters"] == []


def test_near_threshold_joins_near_duplicates():
    rows = [mem("a", "w1 w2 w3 w4 w5 w6 w7 w8 w9"), mem("b", "w1 w2 w3 w4 w5 w6 w7 w8 w9 w10"),
            mem("c", "z1 z2 z3")]
    assert N.build_report(rows)["duplicate_clusters"] == []
    c = N.build_report(rows, near=0.2)["duplicate_clusters"]
    assert len(c) == 1 and c[0]["ids"] == ["a", "b"]
    assert c[0]["max_pairwise_distance"] == pytest.approx(0.1)


def test_chained_cluster_reports_widest_pair():
    rows = [mem("a", "a b c d e"), mem("b", "b c d e f"), mem("c", "c d e f g")]
    c = N.build_report(rows, near=0.4)["duplicate_clusters"]
    assert len(c) == 1 and c[0]["size"] == 3
    assert c[0]["max_pairwise_distance"] > 0.4


def test_clusters_sorted_largest_first():
    rows = [mem("a", "x"), mem("b", "x"), mem("c", "y"), mem("d", "y"), mem("e", "y")]
    sizes = [c["size"] for c in N.build_report(rows)["duplicate_clusters"]]
    assert sizes == [3, 2]


def test_behavioral_distance_scaled_by_population_range():
    rows = [mem("a", behavior={"speed": 0}), mem("b", behavior={"speed": 10}),
            mem("c", behavior={"speed": 5})]
    nov = N.build_report(rows, k=1)["novelty"]
    assert nov["a"]["behavioral_novelty"] == pytest.approx(0.5)
    assert nov["c"]["behavioral_novelty"] == pytest.approx(0.5)


def test_behavioral_none_without_shared_names():
    rows = [mem("a", behavior={"x": 1}), mem("b", behavior={"y": 2})]
    nov = N.build_report(rows)["novelty"]
    assert nov["a"]["behavioral_novelty"] is None


def test_behavioral_constant_axis_contributes_zero():
    rows = [mem("a", behavior={"x": 3}), mem("b", behavior={"x": 3})]
    assert N.build_report(rows)["novelty"]["a"]["behavioral_novelty"] == 0.0


def test_behavior_ignores_bool_nan_and_text():
    b = N.behavior_of({"behavior": {"a": True, "b": float("nan"), "c": "1", "d": 2}})
    assert b == {"d": 2.0}


def test_structural_and_behavioral_not_combined():
    r = N.build_report([mem("a", "x", behavior={"s": 0}), mem("b", "y", behavior={"s": 1})])
    assert set(r["novelty"]["a"]) >= {"structural_novelty", "behavioral_novelty"}
    assert "novelty" not in r["novelty"]["a"]


def test_qd_best_per_niche_and_score():
    rows = [mem("a", "x", niche="n1", quality=0.4), mem("b", "y", niche="n1", quality=0.9),
            mem("c", "z", niche="n2", quality=0.5)]
    qd = N.build_report(rows)["quality_diversity"]
    assert qd["niches"]["n1"]["best_id"] == "b" and qd["niches"]["n1"]["best_quality"] == 0.9
    assert qd["qd_score"] == pytest.approx(1.4)
    assert qd["covered_niches"] == ["n1", "n2"]


def test_qd_tie_goes_to_smaller_id_regardless_of_order():
    rows = [mem("b", "y", niche="n", quality=1), mem("a", "x", niche="n", quality=1)]
    for order in (rows, list(reversed(rows))):
        assert N.build_report(order)["quality_diversity"]["niches"]["n"]["best_id"] == "a"


def test_qd_unassigned_listed_but_not_covered_or_scored():
    rows = [mem("a", "x", quality=9), mem("b", "y", niche="n", quality=1)]
    qd = N.build_report(rows)["quality_diversity"]
    assert qd["niches"][N.UNASSIGNED]["best_quality"] == 9
    assert qd["covered_niches"] == ["n"] and qd["qd_score"] == 1


def test_qd_missing_quality_is_not_zero():
    qd = N.build_report([mem("a", "x", niche="n")])["quality_diversity"]
    assert qd["niches"]["n"]["best_quality"] is None and qd["niches"]["n"]["with_quality"] == 0
    assert qd["qd_score"] == 0 and qd["covered_niches"] == ["n"]


def test_qd_bool_and_nan_quality_ignored():
    rows = [mem("a", "x", niche="n", quality=True), mem("b", "y", niche="n", quality=float("nan"))]
    assert N.build_report(rows)["quality_diversity"]["niches"]["n"]["with_quality"] == 0


def test_coverage_with_declared_niches():
    rows = [mem("a", "x", niche="n1", quality=1)]
    qd = N.build_report(rows, niches=["n1", "n2", " n3 ", "n2", 7])["quality_diversity"]
    assert qd["coverage"] == pytest.approx(1 / 3)
    assert qd["missing_niches"] == ["n2", "n3"]


def test_coverage_none_without_declared_niches():
    assert N.build_report([mem("a", "x", niche="n")])["quality_diversity"]["coverage"] is None


def test_report_independent_of_input_order():
    rows = [mem("a", "x y", niche="n", quality=1, behavior={"s": 1}),
            mem("b", "x y", niche="m", quality=2, behavior={"s": 4}),
            mem("c", "q", niche="n", quality=3, behavior={"s": 9})]
    r1 = N.build_report(rows)
    for r in (r1,):
        r.pop("skipped")
    r2 = N.build_report(list(reversed(rows)))
    r2.pop("skipped")
    for rep in (r1, r2):
        for v in rep["novelty"].values():
            v.pop("record_index")
    assert N.to_json(r1) == N.to_json(r2)


def test_pure_does_not_mutate_input():
    rows = [mem("a", "x", niche="n", quality=1, behavior={"s": 1}), "junk", mem("b", "y")]
    before = copy.deepcopy(rows)
    N.build_report(rows, niches=["n"])
    assert rows == before


def test_to_json_is_stable_and_parses():
    r = N.build_report([mem("a", "x"), mem("b", "x")])
    assert N.to_json(r) == N.to_json(copy.deepcopy(r))
    assert json.loads(N.to_json(r))["members"] == 2


def test_japanese_text_parts_work():
    r = N.build_report([mem("a", "調査 要約"), mem("b", "調査 要約")])
    assert r["duplicate_clusters"][0]["ids"] == ["a", "b"]


def test_module_does_not_import_io_or_network():
    src = open(os.path.join(REPO, "relay", "org", "novelty.py"), encoding="utf-8").read()
    for banned in ("import os", "import subprocess", "import socket", "import time",
                   "open(", "urllib", "requests"):
        assert banned not in src


def test_no_runtime_module_imports_novelty():
    hits = []
    for root, dirs, files in os.walk(os.path.join(REPO, "relay")):
        for f in files:
            if f.endswith(".py") and f != "novelty.py":
                text = open(os.path.join(root, f), encoding="utf-8", errors="replace").read()
                if "org import novelty" in text or "org.novelty" in text:
                    hits.append(f)
    assert hits == []


# ---- CLI ----------------------------------------------------------------------------------

def run_cli(*args):
    p = subprocess.run([sys.executable, "-I", "-X", "utf8", SCRIPT, *args],
                       capture_output=True, cwd=REPO)
    return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write((r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)) + "\n")


def test_cli_text_and_json(tmp_path):
    f = tmp_path / "pop.jsonl"
    write_jsonl(f, [mem("a", "調査 x", niche="n", quality=1), mem("b", "調査 x"), "not json", ""])
    code, out, _ = run_cli(str(f))
    assert code == 0 and "3 records, 1 malformed, 2 members" in out and "cluster: a, b" in out
    code, out, _ = run_cli(str(f), "--format", "json", "--niche", "n", "--niche", "m")
    rep = json.loads(out)
    assert code == 0 and rep["quality_diversity"]["coverage"] == 0.5


def test_cli_writes_nothing_without_out(tmp_path):
    f = tmp_path / "pop.jsonl"
    write_jsonl(f, [mem("a", "x")])
    before = sorted(os.listdir(tmp_path))
    run_cli(str(f))
    assert sorted(os.listdir(tmp_path)) == before


def test_cli_out_writes_only_that_path(tmp_path):
    f = tmp_path / "pop.jsonl"
    write_jsonl(f, [mem("a", "x"), mem("b", "x")])
    o = tmp_path / "report.txt"
    code, out, _ = run_cli(str(f), "--out", str(o))
    assert code == 0 and sorted(os.listdir(tmp_path)) == ["pop.jsonl", "report.txt"]
    assert o.read_bytes().decode("utf-8") == out


def test_cli_refuses_missing_file_and_bad_options(tmp_path):
    code, _, err = run_cli(str(tmp_path / "nope.jsonl"))
    assert code == 2 and "refused" in err
    f = tmp_path / "pop.jsonl"
    write_jsonl(f, [mem("a", "x")])
    assert run_cli(str(f), "-k", "0")[0] == 2
    assert run_cli(str(f), "--near", "1.5")[0] == 2


def test_cli_refuses_unwritable_out(tmp_path):
    f = tmp_path / "pop.jsonl"
    write_jsonl(f, [mem("a", "x")])
    code, _, err = run_cli(str(f), "--out", str(tmp_path / "no_dir" / "r.txt"))
    assert code == 2 and "refused" in err
