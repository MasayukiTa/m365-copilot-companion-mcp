# -*- coding: utf-8 -*-
"""scripts/phase_c_exit_report.py: Phase C exit verification (slice C8). A paired static-baseline
(A) vs dynamic-coordination (B) comparison of quality and cost PER TASK KIND, built on ab_compare's
per-run measures and load_routing_pairwise's router-acted check. These tests use synthetic .fleet
records only; they assert the task-kind grouping, the paired interval, and the explicit missing-
pairs line, and that the script writes only its --out file (read-only everywhere else).
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from scripts import ab_compare as AB  # noqa: E402
from scripts import load_routing_pairwise as LP  # noqa: E402
from scripts import load_routing_report as LRR  # noqa: E402
from scripts import phase_c_exit_report as C8  # noqa: E402


# ----------------------------------------------------------------- building a tiny fleet (as C3)

def _run_workers(run_id, outcomes, turns, phase=((10.0, 25.0),)):
    ws = []
    for i, (o, t) in enumerate(zip(outcomes, turns)):
        ev = [{"ts": x} for x in (phase[i] if i < len(phase) else ())]
        ws.append({"run_id": run_id, "name": "w%d" % i, "outcome": o, "turn": t,
                   "phase_events": ev})
    return ws


def _entry(goal, cond, run_id, kind, exp="exp-1"):
    return {"goal_id": goal, "condition": cond, "experiment_id": exp, "run_id": run_id,
            "task_kind": kind}


def _shadow_rows(*specs):
    rows = []
    for rid, mode, differs in specs:
        rows.append({"v": 1, "ts": 1.0, "run_id": rid, "mode": mode, "differs": differs,
                     "actual": {"name": "w0", "role": "producer"},
                     "shadow": {"name": "w1", "role": "subtask"},
                     "comparable": True, "queue_len": 2})
    return rows


def _write_fleet(tmp_path, workers, shadow_rows):
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    (fleet / "status.json").write_text(json.dumps({"workers": workers}), encoding="utf-8")
    (fleet / "tool_events.jsonl").write_text("", encoding="utf-8")
    (fleet / "campaigns.jsonl").write_text("", encoding="utf-8")
    with open(fleet / LRR.LOG_NAME, "w", encoding="utf-8") as fh:
        for r in shadow_rows:
            fh.write(json.dumps(r) + "\n")
    return str(fleet)


def _pair(goal, kind, b_turns=2, b_wall=20.0, a_turns=5, a_wall=50.0, exp="exp-1",
          b_outcome="DONE"):
    """A settled baseline (A) run and a settled coordinated (B) run for one goal, both of one kind.
    By default B is cheaper (fewer turns, shorter wall) and the B side genuinely reordered."""
    ra, rb = "rA-" + goal, "rB-" + goal
    ws = _run_workers(ra, ["DONE", "DONE"], [a_turns, a_turns],
                      phase=((0.0, a_wall), (0.0, a_wall)))
    ws += _run_workers(rb, [b_outcome, b_outcome], [b_turns, b_turns],
                       phase=((0.0, b_wall), (0.0, b_wall)))
    es = [_entry(goal, "A", ra, kind, exp), _entry(goal, "B", rb, kind, exp)]
    sh = _shadow_rows((rb, "on", True))               # B acted; A has no on row
    return ws, es, sh


def _entries(es):
    return {(e["goal_id"], e["condition"]): e for e in es}


def _build(tmp_path, pairs, min_pairs=5, use_router_action=True):
    """Assemble several _pair()s into one fleet + manifest and run compare() over it."""
    ws, es, sh = [], [], []
    for w, e, s in pairs:
        ws += w
        es += e
        sh += s
    fleet = _write_fleet(tmp_path, ws, sh)
    entries = _entries(es)
    for key, entry in entries.items():       # attach task_kind the way load_manifest would
        entry["task_kind"] = entry.get("task_kind", C8.UNSPECIFIED)
    action = LP.router_action_by_run(fleet)
    rep = C8.compare(entries, [], ws, [], action, min_pairs=min_pairs,
                     use_router_action=use_router_action)
    return rep, fleet


# ----------------------------------------------------------------- task-kind grouping and deltas

def test_pairs_are_grouped_by_task_kind_with_the_known_delta(tmp_path):
    rep, _ = _build(tmp_path, [_pair("g1", "refactor"), _pair("g2", "bugfix")], min_pairs=1)
    assert set(rep["kinds"]) == {"refactor", "bugfix"}
    assert rep["kinds"]["refactor"]["complete_pairs"] == 1
    # B cheaper by hand: turns 2-5=-3, wall 20-50=-30; both sides DONE so quality delta 0
    d = rep["kinds"]["refactor"]["pairs"][0]["delta_B_minus_A"]
    assert d["turns"] == -6 and d["wall_s"] == -30 and d["success"] == 0
    assert rep["kinds"]["refactor"]["cost_delta"]["turns"]["mean"] == -6
    assert rep["kinds"]["refactor"]["quality_delta"]["mean"] == 0


def test_quality_delta_is_success_and_turns_is_cost(tmp_path):
    # a kind where B fails (ABANDONED) while A succeeds: quality delta is -1
    rep, _ = _build(tmp_path, [_pair("g1", "doc", b_outcome="ABANDONED")], min_pairs=1)
    assert rep["kinds"]["doc"]["quality_delta"]["mean"] == -1
    assert "turns" in rep["kinds"]["doc"]["cost_delta"]
    assert "success" not in rep["kinds"]["doc"]["cost_delta"]


# ----------------------------------------------------------------- the paired interval

def test_interval_is_null_for_a_single_pair_and_real_for_several(tmp_path):
    # one pair: mean present, interval null (no spread). Each _build() makes its own 'fleet'
    # directory, so give the two runs separate tmp subdirs.
    (tmp_path / "one").mkdir()
    (tmp_path / "many").mkdir()
    rep1, _ = _build(tmp_path / "one", [_pair("g1", "refactor")], min_pairs=1)
    q = rep1["kinds"]["refactor"]["cost_delta"]["turns"]
    assert q["n"] == 1 and q["mean"] == -6 and q["ci95_low"] is None and q["ci95_high"] is None
    # three pairs with varying B cost: a real interval around the mean of the paired diffs
    pairs = [_pair("g1", "refactor", b_turns=2), _pair("g2", "refactor", b_turns=3),
             _pair("g3", "refactor", b_turns=4)]
    rep3, _ = _build(tmp_path / "many", pairs, min_pairs=1)
    t = rep3["kinds"]["refactor"]["cost_delta"]["turns"]
    # per pair turns delta = 2*(b_turns-5): -6, -4, -2 -> mean -4
    assert t["n"] == 3 and abs(t["mean"] - (-4)) < 1e-9
    assert t["ci95_low"] is not None and t["ci95_low"] < t["mean"] < t["ci95_high"]


def test_mean_ci_matches_a_hand_computed_interval():
    # diffs -6,-4,-2: mean -4, sd 2, n=3, t95(df=2)=4.303 -> half = 4.303*2/sqrt(3)
    import math
    mean, lo, hi, n = C8._mean_ci([-6, -4, -2])
    half = 4.303 * 2.0 / math.sqrt(3)
    assert n == 3 and abs(mean - (-4)) < 1e-9
    assert abs(lo - (-4 - half)) < 1e-6 and abs(hi - (-4 + half)) < 1e-6


# ----------------------------------------------------------------- the missing-pairs line / exit

def test_missing_line_names_each_short_kind_and_its_goals(tmp_path):
    # refactor has 2 complete pairs (needs 5 -> 3 missing); bugfix has 1 (needs 5 -> 4 missing)
    pairs = [_pair("r1", "refactor"), _pair("r2", "refactor"), _pair("b1", "bugfix")]
    rep, _ = _build(tmp_path, pairs, min_pairs=5)
    assert rep["verdict"] == C8.UNMET
    line = C8.missing_line(rep)
    assert line.startswith("MISSING:")
    assert "refactor need 3 more (have 2 of 5)" in line
    assert "bugfix need 4 more (have 1 of 5)" in line


def test_incomplete_goal_is_named_in_the_missing_line(tmp_path):
    # g2's B never acted -> incomplete; it must be listed under its kind in the MISSING line
    ws, es, _ = _pair("g2", "refactor")
    sh = _shadow_rows(("rB-g2", "on", False))         # on but did not reorder
    rep, _ = _build(tmp_path, [_pair("g1", "refactor"), (ws, es, sh)], min_pairs=5)
    assert any(g["goal_id"] == "g2" and "router_did_not_act" in g["reason"]
              for g in rep["incomplete_goals"])
    line = C8.missing_line(rep)
    assert "g2" in line and "refactor" in line


def test_exit_condition_met_when_every_kind_reaches_the_bar(tmp_path):
    pairs = [_pair("g%d" % i, "refactor") for i in range(5)]
    rep, _ = _build(tmp_path, pairs, min_pairs=5)
    assert rep["verdict"] == C8.MET
    assert rep["kinds"]["refactor"]["meets_exit"] is True
    assert C8.missing_line(rep) == \
        "MISSING: none. Every task kind has >=5 complete pairs; exit condition met."


# ----------------------------------------------------------------- pairing rules (reused from C0/C3)

def test_a_kind_mismatch_between_sides_is_excluded(tmp_path):
    ws, es, sh = _pair("g1", "refactor")
    es[1]["task_kind"] = "bugfix"                      # B declares a different kind
    rep, _ = _build(tmp_path, [(ws, es, sh)], min_pairs=1)
    assert rep["total_complete_pairs"] == 0
    assert any("task_kind differs" in g["reason"] for g in rep["incomplete_goals"])


def test_a_b_side_that_never_acted_is_incomplete(tmp_path):
    ws, es, _ = _pair("g1", "refactor")
    sh = _shadow_rows(("rB-g1", "on", False))
    rep, _ = _build(tmp_path, [(ws, es, sh)], min_pairs=1)
    assert rep["total_complete_pairs"] == 0
    assert "router_did_not_act" in rep["incomplete_goals"][0]["reason"]


def test_experiment_id_mismatch_blocks_the_pair(tmp_path):
    ws, es, sh = _pair("g1", "refactor")
    es[1]["experiment_id"] = "exp-2"
    rep, _ = _build(tmp_path, [(ws, es, sh)], min_pairs=1)
    assert rep["total_complete_pairs"] == 0
    assert "experiment_id" in rep["incomplete_goals"][0]["reason"]


def test_no_router_action_pairs_on_settled_and_experiment_id_alone(tmp_path):
    # with the router check off, a B side that never acted still pairs (plain C0)
    ws, es, _ = _pair("g1", "refactor")
    sh = _shadow_rows(("rB-g1", "on", False))
    rep, _ = _build(tmp_path, [(ws, es, sh)], min_pairs=1, use_router_action=False)
    assert rep["total_complete_pairs"] == 1


# ----------------------------------------------------------------- manifest + main (read-only)

def test_load_manifest_reads_task_kind_and_is_the_ab_shape(tmp_path):
    man = [_entry("g1", "A", "rA", "refactor"), _entry("g1", "B", "rB", "refactor"),
           {"goal_id": "g2", "condition": "A", "experiment_id": "exp-1", "run_id": "r2"}]
    p = tmp_path / "m.json"
    p.write_text(json.dumps(man), encoding="utf-8")
    entries, probs = C8.load_manifest(str(p))
    assert probs == []
    assert entries[("g1", "A")]["task_kind"] == "refactor"
    assert entries[("g2", "A")]["task_kind"] == C8.UNSPECIFIED      # default when omitted
    # the base ab_compare loader accepts the very same manifest
    ab_entries, ab_probs = AB.load_manifest(str(p))
    assert ab_probs == [] and sorted(ab_entries) == sorted(entries)


def test_main_writes_only_its_out_file_and_emits_json_then_text(tmp_path, capsys):
    ws, es, sh = _pair("g1", "refactor")
    fleet = _write_fleet(tmp_path, ws, sh)
    man = tmp_path / "m.json"
    man.write_text(json.dumps(es), encoding="utf-8")
    out = tmp_path / "report.txt"

    def snapshot():
        return {n: open(os.path.join(fleet, n), "rb").read()
                for n in sorted(os.listdir(fleet))}
    before = snapshot()
    rc = C8.main(["--manifest", str(man), "--fleet-dir", fleet, "--min-pairs", "1",
                  "--out", str(out)])
    assert rc == 0
    printed = capsys.readouterr().out
    head, _, tail = printed.partition("\n# ")
    rep = json.loads(head)
    assert rep["verdict"] == C8.MET
    assert rep["kinds"]["refactor"]["pairs"][0]["delta_B_minus_A"]["turns"] == -6
    assert "Phase C exit verification" in tail and "MISSING:" in tail
    # the fleet is untouched; the only new file is the report we asked for
    assert before == snapshot()
    assert out.read_text(encoding="utf-8") == printed


def test_main_without_out_prints_but_writes_nothing(tmp_path, capsys):
    ws, es, sh = _pair("g1", "refactor")
    fleet = _write_fleet(tmp_path, ws, sh)
    man = tmp_path / "m.json"
    man.write_text(json.dumps(es), encoding="utf-8")
    rc = C8.main(["--manifest", str(man), "--fleet-dir", fleet, "--min-pairs", "1",
                  "--format", "text"])
    assert rc == 0
    assert "MISSING:" in capsys.readouterr().out
    assert not (tmp_path / "report.txt").exists()


def test_metrics_match_ab_compare_exactly():
    assert C8.METRICS == AB.METRICS
    assert C8.QUALITY == "success" and "success" not in C8.COST


def test_empty_manifest_reports_no_kinds_and_a_clear_missing_line(tmp_path):
    man = tmp_path / "m.json"
    man.write_text("[]", encoding="utf-8")
    fleet = _write_fleet(tmp_path, [], [])
    rep = C8.build_report(str(man), fleet, min_pairs=5)
    assert rep["kinds"] == {} and rep["verdict"] == C8.UNMET
    assert "no task kinds" in C8.missing_line(rep)
