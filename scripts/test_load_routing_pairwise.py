# -*- coding: utf-8 -*-
"""scripts/load_routing_pairwise.py: the C3 paired comparison. It reuses ab_compare's per-run
outcome measures, and adds the one thing ab_compare cannot do -- refuse a pair whose `on` side
never actually reordered admission, and refuse a queue-order side that did. Read-only throughout.
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from scripts import ab_compare as AB  # noqa: E402
from scripts import load_routing_pairwise as P  # noqa: E402
from scripts import load_routing_report as LRR  # noqa: E402


# ----------------------------------------------------------------- building a tiny fleet

def _run_workers(run_id, outcomes, turns, phase=((10.0, 25.0),)):
    """Workers keyed by run_id only (the shape ab_compare measures from status.json)."""
    ws = []
    for i, (o, t) in enumerate(zip(outcomes, turns)):
        ev = [{"ts": x} for x in (phase[i] if i < len(phase) else ())]
        ws.append({"run_id": run_id, "name": "w%d" % i, "outcome": o, "turn": t,
                   "phase_events": ev})
    return ws


def _entry(goal, cond, run_id, exp="exp-1"):
    return {"goal_id": goal, "condition": cond, "experiment_id": exp, "run_id": run_id}


def _entries(*es):
    return {(e["goal_id"], e["condition"]): e for e in es}


def _shadow_rows(*specs):
    """specs: (run_id, mode, differs). One mode=on differs=True row makes a run 'acted'."""
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


# ----------------------------------------------------------------- router action from the log

def test_router_action_reads_only_on_rows_and_needs_a_differs_row(tmp_path):
    fleet = _write_fleet(tmp_path, [], _shadow_rows(
        ("rB", "on", True), ("rB", "on", False),     # acted: one differs row is enough
        ("rC", "on", False),                          # on but never reordered
        ("rS", "shadow", True),                       # shadow rows are not 'on' -> ignored
        ("", "on", True)))                            # no run_id -> ignored
    act = P.router_action_by_run(fleet)
    assert act["rB"] == {"on": True, "acted": True}
    assert act["rC"] == {"on": True, "acted": False}
    assert "rS" not in act and "" not in act


def test_router_action_on_a_missing_log_is_empty_not_an_error(tmp_path):
    fleet = tmp_path / "empty"
    fleet.mkdir()
    assert P.router_action_by_run(str(fleet)) == {}


# ----------------------------------------------------------------- side labelling

def test_a_b_side_that_never_acted_is_rejected_as_router_did_not_act():
    action = {"rB": {"on": True, "acted": False}}
    ok, why = P._label_side(_entry("g", "B", "rB"), action)
    assert ok is False and "router_did_not_act" in why


def test_a_b_side_with_no_on_rows_is_rejected():
    ok, why = P._label_side(_entry("g", "B", "rB"), {})
    assert ok is False and "no mode=on" in why


def test_an_a_side_that_actually_reordered_is_rejected_as_mislabelled():
    action = {"rA": {"on": True, "acted": True}}
    ok, why = P._label_side(_entry("g", "A", "rA"), action)
    assert ok is False and "mislabelled" in why


def test_a_clean_a_and_b_pass():
    action = {"rA": {"on": False, "acted": False}, "rB": {"on": True, "acted": True}}
    assert P._label_side(_entry("g", "A", "rA"), action) == (True, "")
    assert P._label_side(_entry("g", "B", "rB"), action) == (True, "")


# ----------------------------------------------------------------- the comparison end to end

def _ok_pair(goal, exp="exp-1"):
    """A settled queue-order run and a settled router run for one goal, B cheaper by hand."""
    ra, rb = "rA-" + goal, "rB-" + goal
    ws = _run_workers(ra, ["DONE", "DONE"], [5, 5], phase=((0.0, 50.0), (0.0, 50.0)))
    ws += _run_workers(rb, ["DONE", "DONE"], [2, 2], phase=((0.0, 20.0), (0.0, 20.0)))
    es = [_entry(goal, "A", ra, exp), _entry(goal, "B", rb, exp)]
    sh = _shadow_rows((rb, "on", True))               # B acted; A has no on row
    return ws, es, sh


def test_a_known_pair_measures_the_delta_and_counts_it_as_acted(tmp_path):
    ws, es, sh = _ok_pair("g1")
    action = P.router_action_by_run(_write_fleet(tmp_path, ws, sh))
    rep = P.compare(_entries(*es), [], ws, [], action, min_pairs=1)
    assert rep["verdict"] == "DIRECTION_ONLY"
    assert rep["complete_pairs"] == 1
    d = rep["pairs"][0]["delta_B_minus_A"]
    assert d["turns"] == -6 and d["wall_s"] == -30 and d["success"] == 0


def test_a_pair_whose_b_never_acted_is_incomplete_not_measured(tmp_path):
    ws, es, _ = _ok_pair("g1")
    sh = _shadow_rows(("rB-g1", "on", False))         # on, but the router agreed with queue order
    action = P.router_action_by_run(_write_fleet(tmp_path, ws, sh))
    rep = P.compare(_entries(*es), [], ws, [], action, min_pairs=1)
    assert rep["complete_pairs"] == 0
    assert "router_did_not_act" in rep["incomplete_goals"][0]["reason"]


def test_five_goals_whose_b_never_acted_are_no_complete_pairs(tmp_path):
    # Five goals whose B side is on but never reordered: not one is a complete pair, because a B
    # side that never acted has no routing effect to measure. So the verdict is INSUFFICIENT, and
    # every goal is listed with router_did_not_act -- the point of this script over ab_compare.
    ws, es, sh = [], [], []
    for i in range(5):
        g = "g%d" % i
        w, e, _ = _ok_pair(g)
        ws += w
        es += e
        sh += _shadow_rows(("rB-" + g, "on", False))
    action = P.router_action_by_run(_write_fleet(tmp_path, ws, sh))
    rep = P.compare(_entries(*es), [], ws, [], action, min_pairs=5)
    assert rep["complete_pairs"] == 0 and rep["verdict"] == "INSUFFICIENT"
    assert len(rep["incomplete_goals"]) == 5
    assert all("router_did_not_act" in g["reason"] for g in rep["incomplete_goals"])


def test_too_few_acted_pairs_is_insufficient(tmp_path):
    ws, es, sh = _ok_pair("g1")
    action = P.router_action_by_run(_write_fleet(tmp_path, ws, sh))
    rep = P.compare(_entries(*es), [], ws, [], action)      # default min_pairs 5, only 1 pair
    assert rep["verdict"] == "INSUFFICIENT" and rep["complete_pairs"] == 1


def test_a_mislabelled_a_side_takes_the_goal_out(tmp_path):
    ws, es, sh = _ok_pair("g1")
    # make the A side ALSO an acted on-run: now it is mislabelled, pair is dropped
    sh += _shadow_rows(("rA-g1", "on", True))
    action = P.router_action_by_run(_write_fleet(tmp_path, ws, sh))
    rep = P.compare(_entries(*es), [], ws, [], action, min_pairs=1)
    assert rep["complete_pairs"] == 0
    assert "mislabelled" in rep["incomplete_goals"][0]["reason"]


def test_experiment_id_mismatch_still_blocks_the_pair(tmp_path):
    ws, es, sh = _ok_pair("g1")
    es = [es[0], dict(es[1], experiment_id="exp-2")]        # B carries a different experiment id
    action = P.router_action_by_run(_write_fleet(tmp_path, ws, sh))
    rep = P.compare(_entries(*es), [], ws, [], action, min_pairs=1)
    assert rep["complete_pairs"] == 0
    assert "experiment_id" in rep["incomplete_goals"][0]["reason"]


# ----------------------------------------------------------------- the manifest and main

def test_the_manifest_is_the_ab_compare_shape(tmp_path):
    # the same loader, so an operator can hand one manifest to either tool
    man = [_entry("g1", "A", "rA"), _entry("g1", "B", "rB")]
    p = tmp_path / "m.json"
    p.write_text(json.dumps(man), encoding="utf-8")
    es, probs = AB.load_manifest(str(p))
    assert sorted(es) == [("g1", "A"), ("g1", "B")] and probs == []


def test_main_is_read_only_and_emits_json_then_text(tmp_path, capsys):
    ws, es, sh = _ok_pair("g1")
    fleet = _write_fleet(tmp_path, ws, sh)
    man = tmp_path / "m.json"
    man.write_text(json.dumps(es), encoding="utf-8")
    def snapshot():
        return {n: open(os.path.join(fleet, n), "rb").read()
                for n in sorted(os.listdir(fleet))}
    before = snapshot()
    assert P.main(["--manifest", str(man), "--fleet-dir", fleet, "--min-pairs", "1"]) == 0
    out = capsys.readouterr().out
    head, _, tail = out.partition("\n# ")
    rep = json.loads(head)
    assert rep["verdict"] == "DIRECTION_ONLY" and rep["pairs"][0]["delta_B_minus_A"]["turns"] == -6
    assert "queue order" in tail and "Direction-only" in tail
    assert before == snapshot()


def test_the_script_and_the_recorder_agree_on_the_log_name():
    from relay import load_routing
    assert os.path.basename(load_routing.LOG) == LRR.LOG_NAME


def test_the_two_scripts_share_the_same_outcome_metrics():
    # the paired comparison reports exactly ab_compare's metric set: no private, unverified column
    assert P.METRICS == AB.METRICS
