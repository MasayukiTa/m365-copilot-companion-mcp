# -*- coding: utf-8 -*-
"""scripts/ab_compare.py on synthetic fleets, including one known-answer case worked by hand."""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from scripts import ab_compare as AB  # noqa: E402

_ID = [0]


def _calls(cid, spans):
    """Child calls of one campaign; spans = [(call_ts, duration)] -> call + outcome rows."""
    rows = []
    for i, (ts, dur) in enumerate(spans):
        _ID[0] += 1
        eid = "e%d" % _ID[0]
        rows.append({"event": "call", "id": eid, "ts": ts, "tool": "read_file",
                     "campaign_id": cid, "subtask_id": "%s-%d" % (cid, i % 2 + 1),
                     "role": "subtask", "args": {"p": {"sha16": "h%d" % _ID[0]}}})
        rows.append({"event": "outcome", "id": eid, "ts": ts + dur, "ok": True})
    return rows


def _workers(cid, outcomes, turns, merge_turn=None):
    ws = [{"campaign_id": cid, "role": "subtask", "task_id": "%s-%d" % (cid, i + 1),
           "outcome": o, "turn": t, "name": "w%d" % i, "run_id": "r" + cid}
          for i, (o, t) in enumerate(zip(outcomes, turns))]
    if merge_turn is not None:
        ws.append({"campaign_id": cid, "role": "aggregator", "task_id": cid + "-m",
                   "outcome": "DONE", "turn": merge_turn, "name": "m", "run_id": "r" + cid})
    return ws


def _ledger(cid, n, requeues=0, merged=True):
    rows = [{"kind": "campaign", "campaign_id": cid, "goal": "g", "n": n}]
    rows += [{"kind": "child_requeued", "campaign_id": cid} for _ in range(requeues)]
    if merged:
        rows += [{"kind": "merged", "campaign_id": cid},
                 {"kind": "merge_done", "campaign_id": cid}]
    return rows


def _entry(goal, cond, cid, exp="exp-1"):
    return {"goal_id": goal, "condition": cond, "experiment_id": exp, "campaign_id": cid}


def _entries(*es):
    return {(e["goal_id"], e["condition"]): e for e in es}


def _known_answer_fleet():
    ev, ws, led = [], [], []
    # g1 A: 2 DONE children + merge, turns 3+5+2=10, 4 calls spanning 100..160, 1 requeue
    ev += _calls("cA1", [(100, 10), (120, 10), (140, 10), (150, 10)])
    ws += _workers("cA1", ["DONE", "DONE"], [3, 5], merge_turn=2)
    led += _ledger("cA1", 2, requeues=1)
    # g1 B: success, turns 2+2+1=5, 2 calls spanning 200..220, no requeue
    ev += _calls("cB1", [(200, 5), (210, 10)])
    ws += _workers("cB1", ["DONE", "DONE"], [2, 2], merge_turn=1)
    led += _ledger("cB1", 2)
    # g2 A: one child FAILED, no merge, turns 3+1=4, 3 calls spanning 300..350
    ev += _calls("cA2", [(300, 10), (320, 10), (330, 20)])
    ws += _workers("cA2", ["DONE", "FAILED"], [3, 1])
    led += _ledger("cA2", 2, merged=False)
    # g2 B: success, turns 2+3+1=6, 5 calls spanning 400..460, 2 requeues
    ev += _calls("cB2", [(400, 10), (410, 10), (420, 10), (430, 10), (440, 20)])
    ws += _workers("cB2", ["DONE", "DONE"], [2, 3], merge_turn=1)
    led += _ledger("cB2", 2, requeues=2)
    return ev, ws, led


def test_known_answer_two_goals_by_hand():
    ev, ws, led = _known_answer_fleet()
    es = _entries(_entry("g1", "A", "cA1"), _entry("g1", "B", "cB1"),
                  _entry("g2", "A", "cA2"), _entry("g2", "B", "cB2"))
    rep = AB.compare(es, ev, ws, led, min_pairs=2)
    assert rep["verdict"] == "DIRECTION_ONLY" and rep["complete_pairs"] == 2
    d1, d2 = rep["pairs"]
    assert d1["delta_B_minus_A"] == {"success": 0, "turns": -5, "wall_s": -40,
                                     "tool_calls": -2, "retries": -1}
    assert d2["delta_B_minus_A"] == {"success": 1, "turns": 2, "wall_s": 10,
                                     "tool_calls": 2, "retries": 2}
    md = rep["mean_paired_delta_B_minus_A"]
    assert md["success"]["mean"] == 0.5 and md["turns"]["mean"] == -1.5
    assert md["wall_s"]["mean"] == -15 and md["tool_calls"]["mean"] == 0
    assert md["retries"]["mean"] == 0.5 and all(md[m]["n"] == 2 for m in AB.METRICS)
    a, b = rep["conditions"]["A"], rep["conditions"]["B"]
    assert (a["success_rate"], b["success_rate"]) == (0.5, 1.0)
    assert (a["metrics"]["turns"]["mean"], b["metrics"]["turns"]["mean"]) == (7, 5.5)
    assert (a["metrics"]["wall_s"]["mean"], b["metrics"]["wall_s"]["mean"]) == (55, 40)
    assert (a["metrics"]["tool_calls"]["mean"], b["metrics"]["tool_calls"]["mean"]) == (3.5, 3.5)
    assert (a["metrics"]["retries"]["mean"], b["metrics"]["retries"]["mean"]) == (0.5, 1.0)
    assert "Direction-only" in rep["note"]


def test_fewer_than_n_complete_pairs_is_insufficient_by_default():
    ev, ws, led = _known_answer_fleet()
    es = _entries(_entry("g1", "A", "cA1"), _entry("g1", "B", "cB1"),
                  _entry("g2", "A", "cA2"), _entry("g2", "B", "cB2"))
    rep = AB.compare(es, ev, ws, led)
    assert rep["min_pairs"] == 5 and rep["verdict"] == "INSUFFICIENT"
    assert rep["complete_pairs"] == 2
    assert "direction" in AB.render_text(dict(rep, problems=[])).lower()


def test_unsettled_side_makes_the_goal_incomplete():
    ev, ws, led = _known_answer_fleet()
    led = [r for r in led if not (r.get("campaign_id") == "cB2" and r["kind"] == "merge_done")]
    ws = [w for w in ws if not (w["campaign_id"] == "cB2" and w["role"] == "aggregator")]
    es = _entries(_entry("g1", "A", "cA1"), _entry("g1", "B", "cB1"),
                  _entry("g2", "A", "cA2"), _entry("g2", "B", "cB2"))
    rep = AB.compare(es, ev, ws, led, min_pairs=1)
    assert rep["complete_pairs"] == 1
    assert rep["incomplete_goals"][0]["goal_id"] == "g2"
    assert rep["conditions"]["B"]["unsettled"] == 1


def test_experiment_id_mismatch_is_not_paired():
    ev, ws, led = _known_answer_fleet()
    es = _entries(_entry("g1", "A", "cA1", "exp-1"), _entry("g1", "B", "cB1", "exp-2"))
    rep = AB.compare(es, ev, ws, led, min_pairs=1)
    assert rep["complete_pairs"] == 0
    assert "experiment_id" in rep["incomplete_goals"][0]["reason"]


def test_unknown_campaign_and_missing_metrics_are_none_not_zero():
    ev, ws, led = _known_answer_fleet()
    # cL exists only in the campaign ledger: no status workers (turns), no calls
    led += [{"kind": "campaign", "campaign_id": "cL", "n": 1},
            {"kind": "child_result", "campaign_id": "cL", "task_id": "cL-1", "outcome": "DONE"}]
    es = _entries(_entry("g1", "A", "cL"), _entry("g1", "B", "nope"))
    rep = AB.compare(es, ev, ws, led, min_pairs=1)
    assert rep["conditions"]["B"]["unknown_campaign"] == 1
    a = rep["conditions"]["A"]
    assert a["settled"] == 1 and a["success_rate"] == 1.0
    assert a["metrics"]["turns"] == {"mean": None, "n": 0}
    assert a["metrics"]["tool_calls"] == {"mean": None, "n": 0}
    assert a["metrics"]["retries"]["mean"] == 0


def test_delta_skips_metric_missing_on_one_side():
    ev, ws, led = _known_answer_fleet()
    ws = [w for w in ws if w["campaign_id"] != "cA1"]       # A side loses its turns
    led += [{"kind": "child_result", "campaign_id": "cA1", "task_id": "cA1-%d" % i,
             "outcome": "DONE"} for i in (1, 2)]            # ... but the ledger still has results
    es = _entries(_entry("g1", "A", "cA1"), _entry("g1", "B", "cB1"))
    rep = AB.compare(es, ev, ws, led, min_pairs=1)
    assert rep["pairs"][0]["delta_B_minus_A"]["turns"] is None
    assert rep["mean_paired_delta_B_minus_A"]["turns"]["n"] == 0
    assert rep["pairs"][0]["delta_B_minus_A"]["tool_calls"] == -2


def test_manifest_formats_and_problems(tmp_path):
    good = [_entry("g1", "a", "c1"), _entry("g1", "B", "c2")]
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"entries": good}), encoding="utf-8")
    es, probs = AB.load_manifest(str(p))
    assert sorted(es) == [("g1", "A"), ("g1", "B")] and probs == []
    p2 = tmp_path / "m.jsonl"
    p2.write_text("\n".join(json.dumps(e) for e in good), encoding="utf-8")
    assert sorted(AB.load_manifest(str(p2))[0]) == [("g1", "A"), ("g1", "B")]
    bad = good + [_entry("g1", "A", "c9"), {"goal_id": "g2", "condition": "C"}, "x",
                  _entry("g3", "A", "c3")]
    p3 = tmp_path / "bad.json"
    p3.write_text(json.dumps(bad), encoding="utf-8")
    es, probs = AB.load_manifest(str(p3))
    assert sorted(es) == [("g3", "A")]           # g1 given twice -> excluded entirely
    assert len(probs) == 3


def test_run_id_entry_for_a_run_only_in_status():
    ws = [{"run_id": "r1", "name": "w", "outcome": "DONE", "turn": 4,
           "phase_events": [{"ts": 10.0}, {"ts": 25.0}]},
          {"run_id": "r2", "name": "w", "outcome": "", "turn": 1}]
    es = _entries({"goal_id": "g", "condition": "A", "experiment_id": "e", "run_id": "r1"},
                  {"goal_id": "g", "condition": "B", "experiment_id": "e", "run_id": "r2"})
    rep = AB.compare(es, [], ws, [], min_pairs=1)
    assert rep["conditions"]["A"]["settled"] == 1 and rep["conditions"]["B"]["unsettled"] == 1
    assert rep["conditions"]["A"]["metrics"]["wall_s"]["mean"] == 15
    assert rep["conditions"]["A"]["metrics"]["turns"]["mean"] == 4


def test_main_is_read_only_and_emits_json_then_text(tmp_path, capsys):
    ev, ws, led = _known_answer_fleet()
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    (fleet / "tool_events.jsonl").write_text("\n".join(json.dumps(r) for r in ev), "utf-8")
    (fleet / "campaigns.jsonl").write_text("\n".join(json.dumps(r) for r in led), "utf-8")
    (fleet / "status.json").write_text(json.dumps({"workers": ws}), "utf-8")
    man = tmp_path / "m.json"
    man.write_text(json.dumps([_entry("g1", "A", "cA1"), _entry("g1", "B", "cB1")]), "utf-8")
    before = {f.name: f.read_bytes() for f in fleet.iterdir()}
    assert AB.main(["--manifest", str(man), "--fleet-dir", str(fleet), "--min-pairs", "1"]) == 0
    out = capsys.readouterr().out
    head, _, tail = out.partition("\n# ")
    rep = json.loads(head)
    assert rep["verdict"] == "DIRECTION_ONLY" and rep["pairs"][0]["delta_B_minus_A"]["turns"] == -5
    assert "Direction-only" in tail
    assert before == {f.name: f.read_bytes() for f in fleet.iterdir()}
