# -*- coding: utf-8 -*-
"""Reputation with sparse-data, gaming and collusion resistance (slice G5). Synthetic data only."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.org import reputation as R  # noqa: E402

SCRIPT = os.path.join(REPO, "scripts", "org_reputation.py")


def ev(evaluator, subject, score, task=None, ts=None):
    r = {"evaluator_id": evaluator, "subject_id": subject, "score": score}
    if task is not None:
        r["task_id"] = task
    if ts is not None:
        r["timestamp"] = ts
    return r


def agent(report, name):
    return next(a for a in report["agents"] if a["agent_id"] == name)


def codes(report):
    return [f["code"] for f in report["flags"]]


def many_evaluators(subject, n, score, prefix="e"):
    return [ev("%s%d" % (prefix, i), subject, score) for i in range(n)]


def test_empty_and_non_list_pass_with_zero_checked():
    for bad in ([], None, "x", {"a": 1}):
        rep = R.build_report(bad)
        assert rep["passed"] is True and rep["total"] == 0 and rep["checked"] == 0
        assert rep["agents"] == [] and rep["flags"] == []


def test_sparse_single_evaluation_is_shrunk_and_unknown():
    rep = R.build_report([ev("e1", "a", 1.0)])
    a = agent(rep, "a")
    assert a["confidence"] == R.UNKNOWN and a["contributing_evaluators"] == 1
    assert a["reputation"] == round((4 * 0.5 + 1.0) / 5.0, 6)
    assert 0.5 < a["reputation"] < 1.0


def test_sparse_below_minimum_is_unknown_and_at_minimum_is_low():
    below = R.build_report(many_evaluators("a", R.MIN_EVALUATORS - 1, 0.7))
    at = R.build_report(many_evaluators("a", R.MIN_EVALUATORS, 0.7))
    assert agent(below, "a")["confidence"] == R.UNKNOWN
    assert agent(at, "a")["confidence"] == R.LOW


def test_confidence_ok_with_many_evaluators_and_reputation_approaches_mean():
    rep = R.build_report(many_evaluators("a", 40, 0.9))
    a = agent(rep, "a")
    assert a["confidence"] == R.OK and a["contributing_evaluators"] == 40
    assert 0.85 < a["reputation"] <= 0.9


def test_more_data_moves_reputation_further_from_prior():
    few = agent(R.build_report(many_evaluators("a", 2, 0.9)), "a")["reputation"]
    lots = agent(R.build_report(many_evaluators("a", 20, 0.9)), "a")["reputation"]
    assert 0.5 < few < lots < 0.9


def test_self_evaluation_ignored_and_reported():
    rep = R.build_report([ev("a", "a", 1.0), ev("e1", "a", 0.2)])
    assert codes(rep) == [R.SELF_EVAL] and rep["passed"] is False
    a = agent(rep, "a")
    assert a["contributing_evaluators"] == 1 and a["evaluations_counted"] == 1
    assert a["reputation"] == round((2.0 + 0.2) / 5.0, 6)
    assert rep["flags"][0]["index"] == 0


def test_only_self_evaluations_give_no_agent_row():
    rep = R.build_report([ev("a", "a", 1.0)])
    assert rep["agents"] == [] and rep["counted"] == 0 and rep["passed"] is False


def test_evaluator_dominance_is_capped():
    flood = [ev("big", "a", 1.0) for _ in range(50)]
    rep = R.build_report(flood + [ev("e2", "a", 0.0), ev("e3", "a", 0.0)])
    a = agent(rep, "a")
    assert a["effective_weight"] == 3 * R.EVALUATOR_CAP
    assert a["reputation"] == round((2.0 + 1.0) / (4.0 + 3.0), 6)
    capped = [f for f in rep["flags"] if f["code"] == R.CAPPED]
    assert len(capped) == 1 and capped[0]["agents"] == ["big", "a"]
    assert rep["passed"] is True  # informational only


def test_flood_equals_single_vote():
    one = R.build_report([ev("big", "a", 1.0)])
    flood = R.build_report([ev("big", "a", 1.0) for _ in range(30)])
    assert agent(one, "a")["reputation"] == agent(flood, "a")["reputation"]
    assert agent(flood, "a")["contributing_evaluators"] == 1


def test_duplicate_task_evaluation_counts_once():
    rep = R.build_report([ev("e1", "a", 1.0, task="t1"), ev("e1", "a", 0.0, task="t1"),
                          ev("e1", "a", 1.0, task="t2")])
    assert codes(rep) == [R.DUPLICATE, R.CAPPED] and rep["flags"][0]["index"] == 1
    assert agent(rep, "a")["evaluations_counted"] == 2 and rep["passed"] is False


def test_same_pair_without_task_id_is_not_duplicate():
    rep = R.build_report([ev("e1", "a", 1.0), ev("e1", "a", 0.0)])
    assert R.DUPLICATE not in codes(rep)


def test_reciprocal_high_pair_flagged_and_downweighted():
    recs = [ev("a", "b", 0.95), ev("b", "a", 0.9)] + many_evaluators("a", 3, 0.5, "x") \
        + many_evaluators("b", 3, 0.5, "y")
    rep = R.build_report(recs)
    pair = [f for f in rep["flags"] if f["code"] == R.RECIPROCAL]
    assert len(pair) == 1 and pair[0]["agents"] == ["a", "b"]
    assert rep["downweighted_evaluations"] == 2 and rep["passed"] is False
    plain = R.build_report(many_evaluators("a", 3, 0.5, "x") + [ev("q", "a", 0.9)])
    friendly = agent(rep, "a")["reputation"]
    full = R.build_report(many_evaluators("a", 3, 0.5, "x") + [ev("b", "a", 0.9)])
    assert agent(plain, "a")["reputation"] == agent(full, "a")["reputation"]
    assert friendly < agent(full, "a")["reputation"]


def test_one_sided_high_rating_is_not_reciprocal():
    rep = R.build_report([ev("a", "b", 0.95), ev("b", "a", 0.3)])
    assert R.RECIPROCAL not in codes(rep) and rep["passed"] is True


def test_reciprocal_requires_both_sides_high():
    rep = R.build_report([ev("a", "b", 0.95), ev("b", "a", R.HIGH_SCORE - 0.01)])
    assert R.RECIPROCAL not in codes(rep)
    rep = R.build_report([ev("a", "b", R.HIGH_SCORE), ev("b", "a", R.HIGH_SCORE)])
    assert R.RECIPROCAL in codes(rep)


def test_tight_cluster_flagged():
    recs = []
    for x in "abc":
        for y in "abc":
            if x != y:
                recs.append(ev(x, y, 0.95))
    rep = R.build_report(recs)
    cl = [f for f in rep["flags"] if f["code"] == R.CLUSTER]
    assert len(cl) == 1 and cl[0]["agents"] == ["a", "b", "c"]
    assert rep["downweighted_evaluations"] == 6 and rep["passed"] is False


def test_ring_without_reciprocity_is_a_cluster():
    rep = R.build_report([ev("a", "b", 0.9), ev("b", "c", 0.9), ev("c", "a", 0.9)])
    assert R.CLUSTER in codes(rep) and R.RECIPROCAL not in codes(rep)


def test_cluster_that_rates_many_outsiders_is_not_flagged():
    recs = [ev("a", "b", 0.9), ev("b", "c", 0.9), ev("c", "a", 0.9)]
    for x in "abc":
        recs += [ev(x, "out%d" % i, 0.6) for i in range(10)]
    rep = R.build_report(recs)
    assert R.CLUSTER not in codes(rep)


def test_cluster_downweight_lowers_reputation():
    recs = [ev("a", "b", 0.95), ev("b", "c", 0.95), ev("c", "a", 0.95)]
    flagged = agent(R.build_report(recs), "a")["reputation"]
    neutral = agent(R.build_report([ev("z", "a", 0.95)]), "a")["reputation"]
    assert flagged < neutral


def test_malformed_records_skipped_and_counted():
    bad = [None, "x", 5, {}, ev("", "a", 0.5), ev("e", "", 0.5), ev("e", "a", "0.5"),
           ev("e", "a", True), ev("e", "a", 1.5), ev("e", "a", -0.1), ev("e", "a", float("nan")),
           ev("e", "a", float("inf")), {"evaluator_id": "e", "subject_id": "a"},
           ev("e", "a", 0.5, task=""), ev("e", "a", 0.5, ts="1"), ev("e", "a", 0.5, ts=True),
           ev("e", "a", 0.5, ts=1.5), {"evaluator_id": 1, "subject_id": "a", "score": 0.5}]
    rep = R.build_report(bad + [ev("e1", "a", 0.8)])
    assert rep["malformed"] == len(bad) and rep["checked"] == 1 and rep["passed"] is False
    assert all(s["code"] == "malformed" and s["reason"] for s in rep["skipped"])
    assert agent(rep, "a")["evaluations_counted"] == 1


def test_boundary_scores_and_valid_timestamp_accepted():
    rep = R.build_report([ev("e1", "a", 0, ts=0), ev("e2", "a", 1, ts=1700000000),
                          ev("e3", "a", 0.5, task="t")])
    assert rep["malformed"] == 0 and rep["passed"] is True


def test_whitespace_in_ids_is_stripped():
    rep = R.build_report([ev(" a ", "a", 1.0)])
    assert codes(rep) == [R.SELF_EVAL]


def test_ids_are_case_sensitive():
    rep = R.build_report([ev("A", "a", 1.0)])
    assert R.SELF_EVAL not in codes(rep)


def test_clean_report_passes():
    rep = R.build_report(many_evaluators("a", 5, 0.7) + many_evaluators("b", 5, 0.4, "f"))
    assert rep["passed"] is True and rep["flags"] == [] and len(rep["agents"]) == 2


def test_reputation_stays_in_unit_interval():
    for s in (0.0, 1.0):
        rep = R.build_report(many_evaluators("a", 7, s))
        assert 0.0 <= agent(rep, "a")["reputation"] <= 1.0


def test_deterministic_and_input_not_mutated():
    recs = [ev("a", "b", 0.95), ev("b", "a", 0.95), ev("c", "a", 0.4), ev("a", "a", 1.0),
            ev("c", "b", 0.7, task="t"), ev("c", "b", 0.2, task="t"), None]
    before = copy.deepcopy(recs)
    first = R.to_json(R.build_report(recs))
    assert R.to_json(R.build_report(recs)) == first
    assert recs == before


def test_input_order_does_not_change_agent_results():
    recs = many_evaluators("a", 4, 0.8) + many_evaluators("b", 4, 0.3, "f")
    fwd = R.build_report(recs)["agents"]
    rev = R.build_report(list(reversed(recs)))["agents"]
    assert fwd == rev


def test_module_has_no_runtime_importers():
    skip = {os.path.join(REPO, "relay", "org", "reputation.py"), os.path.abspath(__file__),
            SCRIPT}
    for root, dirs, files in os.walk(os.path.join(REPO, "relay")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            p = os.path.join(root, f)
            if f.endswith(".py") and p not in skip and not f.startswith("test_"):
                with open(p, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                assert "org.reputation" not in text and "org import reputation" not in text, p


def run_cli(tmp_path, payload, *extra, raw=None):
    p = tmp_path / "in.json"
    p.write_text(raw if raw is not None else json.dumps(payload), encoding="utf-8")
    return subprocess.run([sys.executable, SCRIPT, "--records", str(p), *extra],
                          capture_output=True, text=True, encoding="utf-8")


def test_cli_pass_text(tmp_path):
    out = run_cli(tmp_path, many_evaluators("a", 4, 0.8))
    assert out.returncode == 0 and "reputation: PASS" in out.stdout and "a:" in out.stdout


def test_cli_fail_json_and_object_form(tmp_path):
    out = run_cli(tmp_path, {"records": [ev("a", "a", 1.0)]}, "--format", "json")
    assert out.returncode == 1
    data = json.loads(out.stdout)
    assert data["passed"] is False and data["flag_counts"][R.SELF_EVAL] == 1


def test_cli_refuses_bad_input(tmp_path):
    assert run_cli(tmp_path, None, raw="not json").returncode == 2
    assert run_cli(tmp_path, {"x": 1}).returncode == 2
    missing = subprocess.run([sys.executable, SCRIPT, "--records", str(tmp_path / "nope.json")],
                             capture_output=True, text=True, encoding="utf-8")
    assert missing.returncode == 2


def test_cli_does_not_modify_input(tmp_path):
    p = tmp_path / "in.json"
    p.write_text(json.dumps(many_evaluators("a", 3, 0.5)), encoding="utf-8")
    before = p.read_bytes()
    subprocess.run([sys.executable, SCRIPT, "--records", str(p)], capture_output=True)
    assert p.read_bytes() == before and sorted(os.listdir(tmp_path)) == ["in.json"]
