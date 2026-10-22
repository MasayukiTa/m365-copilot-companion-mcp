# -*- coding: utf-8 -*-
"""Proposer / solver / judge role separation (slice G3). Synthetic data only."""
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

from relay.org import role_separation as R  # noqa: E402

SCRIPT = os.path.join(REPO, "scripts", "org_role_separation.py")


def cand(cid="c1", proposer="p", solver="s", judge="j", verdict=None, by=None):
    r = {"candidate_id": cid, "proposer_id": proposer, "solver_id": solver, "judge_id": judge}
    if verdict is not None:
        r["verdict"] = verdict
    if by is not None:
        r["verdict_by"] = by
    return r


def codes(report):
    return [v["code"] for v in report["violations"]]


def test_clean_candidate_passes():
    rep = R.build_report([cand(verdict="accept", by="j")])
    assert rep["passed"] is True
    assert rep["violations"] == [] and rep["checked"] == 1 and rep["clean_candidates"] == 1


def test_clean_without_verdict_passes():
    assert R.build_report([cand()])["passed"] is True


def test_empty_and_non_list_input_pass_with_zero_checked():
    for bad in ([], None, "x", {"a": 1}):
        rep = R.build_report(bad)
        assert rep["passed"] is True and rep["checked"] == 0 and rep["total"] == 0


def test_judge_is_proposer():
    rep = R.build_report([cand(judge="p")])
    assert codes(rep) == [R.JUDGE_IS_PROPOSER] and rep["passed"] is False


def test_judge_is_solver():
    assert codes(R.build_report([cand(judge="s")])) == [R.JUDGE_IS_SOLVER]


def test_proposer_equals_solver_equals_judge_raises_both():
    assert codes(R.build_report([cand(proposer="a", solver="a", judge="a")])) == [
        R.JUDGE_IS_PROPOSER, R.JUDGE_IS_SOLVER]


def test_solver_may_equal_proposer_when_judge_differs():
    assert R.build_report([cand(proposer="a", solver="a", judge="j")])["passed"] is True


@pytest.mark.parametrize("judge", [None, "", "   ", 5])
def test_missing_judge(judge):
    r = cand()
    r["judge_id"] = judge
    assert codes(R.build_report([r])) == [R.MISSING_JUDGE]


def test_missing_judge_key_absent():
    r = cand()
    del r["judge_id"]
    assert codes(R.build_report([r])) == [R.MISSING_JUDGE]


def test_missing_judge_does_not_also_flag_verdict():
    assert codes(R.build_report([cand(judge=None, verdict="accept", by="x")])) == [R.MISSING_JUDGE]


def test_judge_descends_from_proposer_direct_and_transitive():
    rep = R.build_report([cand()], [{"from": "p", "to": "j"}])
    assert codes(rep) == [R.JUDGE_DESCENDS]
    rep = R.build_report([cand()], [["p", "m"], ["m", "n"], ["n", "j"]])
    assert codes(rep) == [R.JUDGE_DESCENDS]


def test_judge_ancestor_of_proposer():
    rep = R.build_report([cand()], [["j", "m"], ["m", "p"]])
    assert codes(rep) == [R.JUDGE_ANCESTOR]


def test_unrelated_edges_do_not_flag():
    rep = R.build_report([cand()], [["p", "x"], ["y", "j"], ["x", "z"]])
    assert rep["passed"] is True


def test_cycle_terminates_and_flags_both_directions():
    rep = R.build_report([cand()], [["p", "j"], ["j", "p"]])
    assert codes(rep) == [R.JUDGE_DESCENDS, R.JUDGE_ANCESTOR]


def test_cycle_not_containing_judge_terminates():
    assert R.build_report([cand()], [["p", "a"], ["a", "b"], ["b", "a"]])["passed"] is True


def test_bad_edges_are_skipped():
    edges = ["x", [1], ["a"], {"from": "a"}, {"to": "b"}, ["", "j"], None, ["p", "j", "k"]]
    assert R.build_report([cand()], edges)["passed"] is True
    assert R.build_report([cand()], "not a list")["passed"] is True


def test_edge_ids_are_stripped():
    assert codes(R.build_report([cand()], [[" p ", " j "]])) == [R.JUDGE_DESCENDS]


def test_verdict_not_by_judge():
    rep = R.build_report([cand(verdict="accept", by="p")])
    assert codes(rep) == [R.VERDICT_NOT_BY_JUDGE]
    assert "assigned judge is j" in rep["violations"][0]["reason"]


def test_verdict_unattributed():
    assert codes(R.build_report([cand(verdict="accept")])) == [R.VERDICT_UNATTRIBUTED]


@pytest.mark.parametrize("verdict,expected", [("", []), ("  ", []), (None, []),
                                              (False, [R.VERDICT_UNATTRIBUTED]),
                                              (0, [R.VERDICT_UNATTRIBUTED]),
                                              ({"ok": True}, [R.VERDICT_UNATTRIBUTED])])
def test_what_counts_as_a_recorded_verdict(verdict, expected):
    r = cand()
    r["verdict"] = verdict
    assert codes(R.build_report([r])) == expected


def test_verdict_by_whitespace_is_stripped():
    assert R.build_report([cand(verdict="ok", by=" j ")])["passed"] is True


def test_proposer_judging_itself_and_recording_its_own_verdict():
    rep = R.build_report([cand(judge="p", verdict="accept", by="p")])
    assert codes(rep) == [R.JUDGE_IS_PROPOSER]


def test_absent_proposer_is_not_guessed():
    r = cand()
    del r["proposer_id"]
    assert R.build_report([r], [["p", "j"]])["passed"] is True


def test_malformed_records_counted_and_fail():
    rep = R.build_report(["x", None, 3, cand()])
    assert rep["malformed"] == 3 and rep["checked"] == 1 and rep["passed"] is False
    assert [s["index"] for s in rep["skipped"]] == [0, 1, 2]


def test_counts_cover_every_code_and_match_list():
    rows = [cand("a", judge="p"), cand("b", judge="s"), cand("c", judge=None),
            cand("d", verdict="x", by="q"), cand("e", verdict="x")]
    rep = R.build_report(rows, [["p", "j"]])
    assert set(rep["violation_counts"]) == set(R.CODES)
    assert sum(rep["violation_counts"].values()) == rep["violations_total"] == len(rep["violations"])
    assert rep["flagged_candidates"] + rep["clean_candidates"] == rep["checked"]


def test_violations_sorted_and_deterministic():
    rows = [cand("z", judge="p"), cand("a", judge="s")]
    a = R.build_report(rows)
    b = R.build_report(copy.deepcopy(rows))
    assert [v["candidate_id"] for v in a["violations"]] == ["a", "z"]
    assert R.to_json(a) == R.to_json(b)


def test_input_is_not_mutated():
    rows = [cand(judge="p", verdict="x", by="q")]
    edges = [["p", "j"]]
    before = copy.deepcopy((rows, edges))
    R.build_report(rows, edges)
    assert (rows, edges) == before


def test_to_json_roundtrip():
    rep = R.build_report([cand(judge="p")])
    assert json.loads(R.to_json(rep)) == rep


def run_cli(tmp_path, payload, *extra):
    path = tmp_path / "in.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return subprocess.run([sys.executable, "-I", SCRIPT, "--records", str(path), *extra],
                          capture_output=True, text=True, encoding="utf-8")


def test_cli_pass_exit_zero(tmp_path):
    res = run_cli(tmp_path, [cand(verdict="ok", by="j")])
    assert res.returncode == 0 and "PASS" in res.stdout


def test_cli_fail_exit_one_with_reason(tmp_path):
    res = run_cli(tmp_path, {"records": [cand(judge="p")], "edges": []})
    assert res.returncode == 1 and "FAIL" in res.stdout and R.JUDGE_IS_PROPOSER in res.stdout


def test_cli_edges_and_json_format(tmp_path):
    res = run_cli(tmp_path, {"records": [cand()], "edges": [["p", "j"]]}, "--format", "json")
    out = json.loads(res.stdout)
    assert res.returncode == 1 and out["violation_counts"][R.JUDGE_DESCENDS] == 1


def test_cli_refuses_bad_input(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    res = subprocess.run([sys.executable, "-I", SCRIPT, "--records", str(bad)],
                         capture_output=True, text=True)
    assert res.returncode == 2
    assert run_cli(tmp_path, {"records": "x"}).returncode == 2
    missing = subprocess.run([sys.executable, "-I", SCRIPT, "--records", str(tmp_path / "none.json")],
                             capture_output=True, text=True)
    assert missing.returncode == 2


def test_cli_writes_nothing(tmp_path):
    run_cli(tmp_path, [cand(judge="p")])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["in.json"]
