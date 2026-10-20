# -*- coding: utf-8 -*-
"""scripts/exp_collect.py on a synthetic experiment root: planning, the join of connector-less ledger rows
to fan-out children, duplicate cost with a hand-worked answer, information loss, pair statistics."""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from scripts import exp_collect as C  # noqa: E402

GOALS = ["s%02d" % i for i in range(1, 7)] + ["t%02d" % i for i in range(1, 7)]


def _jl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_schedule_interleaves_families_then_replicates():
    order = C.schedule(GOALS, 20)
    assert [g for g, _ in order[:4]] == ["s01", "t01", "s02", "t02"]
    assert len(order) == 20 and [r for _g, r in order].count(2) == 8
    assert len(set(order)) == 20


def test_plan_alternates_arm_order_and_shares_an_experiment_id_per_pair(tmp_path):
    runs = C.make_plan(GOALS, 4, "exp-x", str(tmp_path))
    assert len(runs) == 8
    assert [r["arm"] for r in runs[:4]] == ["on", "off", "off", "on"]
    for k in range(0, 8, 2):
        assert runs[k]["experiment_id"] == runs[k + 1]["experiment_id"]
        assert {runs[k]["condition"], runs[k + 1]["condition"]} == {"A", "B"}
    assert len({r["label"] for r in runs}) == 8


def test_sign_test_known_values():
    assert C.sign_test_p([]) == 1.0
    assert C.sign_test_p([1, 2, 3, 4, 5, 6]) == 2.0 / 64          # six of six one way
    assert abs(C.sign_test_p([1, 1, 1, 1, 1, -1]) - 14.0 / 64) < 1e-12
    assert C.sign_test_p([0, 0, 1]) == 1.0


def test_bootstrap_is_seeded_and_brackets_the_mean():
    d = [3.0, 1.0, 2.0, 5.0, 4.0, 2.5]
    assert C.bootstrap_ci(d) == C.bootstrap_ci(d)
    lo, hi = C.bootstrap_ci(d)
    assert lo <= sum(d) / len(d) <= hi
    assert C.bootstrap_ci([]) == (None, None)


def _call(i, worker, ts, tool, sha, dur):
    return [{"event": "call", "id": "e%d" % i, "ts": ts, "tool": tool, "worker": worker, "task": "",
             "attr": "explicit", "args": {"cmd": {"sha16": sha}}},
            {"event": "outcome", "id": "e%d" % i, "ts": ts + dur, "ok": True, "dur_mono_s": dur}]


def _write_run(root, label, ledger_path):
    """A fan-out run of 3 children: two issue the same 30 s command, one a different one; the merge
    gets child 3's value wrong while child 1's report carries the right one."""
    sd = os.path.join(root, "runs", label)
    goal = "全体の課題"
    kids = ["%s\n【担当範囲 %d/3】" % (goal * 20, i) for i in (1, 2, 3)]
    camp = [{"kind": "campaign", "campaign_id": "cA", "goal": goal, "n": 3, "run_id": "rX_a0"}]
    camp += [{"campaign_id": "cA", "task_id": "cA-%d" % (i + 1), "subtask_index": i + 1, "text": kids[i]}
             for i in range(3)]
    camp += [{"kind": "child_result", "campaign_id": "cA", "task_id": "cA-%d" % (i + 1), "outcome": "DONE",
              "result": "S0%d=%d" % (i + 1, 100 * (i + 1))} for i in range(3)]
    camp += [{"kind": "merged", "campaign_id": "cA"}, {"kind": "merge_done", "campaign_id": "cA"}]
    _jl(os.path.join(sd, "campaigns.jsonl"), camp)
    t0 = 1000.0
    tr = lambda n, g, ts, texts: [{"meta": True, "key": "rX_a0_" + n, "name": n, "goal": g, "ts": ts}] + \
        [{"turn": i + 1, "role": "assistant", "text": t} for i, t in enumerate(texts)] + \
        [{"turn": 1, "role": "metric", "name": "turn_wait_s", "value": 10.0, "ts": ts + 50}]
    _jl(os.path.join(sd, "transcripts", "rX_a0_w0.jsonl"), tr("w0", goal, t0, ["split"]))
    for i in range(3):
        _jl(os.path.join(sd, "transcripts", "rX_a0_w%d.jsonl" % (i + 1)),
            tr("w%d" % (i + 1), kids[i], t0 + 10, ["S0%d=%d" % (i + 1, 100 * (i + 1))]))
    _jl(os.path.join(sd, "transcripts", "rX_a0_w4.jsonl"),
        tr("w4", "merge prompt", t0 + 200, ["S01=100\nS02=200\nS03=999"]))
    ev = []
    ev += _call(1, "w1", t0 + 20, "shell_exec", "AAAA", 30.0)     # first issuer
    ev += _call(2, "w2", t0 + 21, "shell_exec", "AAAA", 31.0)     # duplicate, 31 s
    ev += _call(3, "w3", t0 + 22, "shell_exec", "BBBB", 12.0)     # different
    ev += _call(4, "w3", t0 + 40, "shell_exec", "AAAA", 29.0)     # duplicate, 29 s
    ev += _call(5, "w4", t0 + 210, "read_file", "CCCC", 0.01)     # merge worker: not a sibling call
    ev += _call(6, "w1", t0 + 25, "call_tool.index", "ZZZZ", 1.0)  # discovery chatter: excluded
    _jl(ledger_path, ev)
    return sd


def _finished(root, label, arm, pair_id="s01#1"):
    return {"label": label, "goal_id": "s01", "arm": arm, "ts_start": 990.0, "ts_end": 1300.0,
            "exit_code": 0, "data_intact": True}


def _goal():
    return {"goal_id": "s01", "family": "series", "expected": {"S01": 100, "S02": 200, "S03": 300}}


def test_run_record_joins_workers_to_children_and_prices_the_duplicates(tmp_path):
    root, ledger = str(tmp_path), str(tmp_path / "ledger.jsonl")
    label = "01a_s01_r1_on"
    sd = _write_run(root, label, ledger)
    run = {"label": label, "pair_id": "s01#1", "goal_id": "s01", "arm": "on", "state_dir": sd}
    rec = C.run_record(run, _goal(), _finished(root, label, "on"), ledger)
    assert rec["split"] and rec["children"] == 3 and rec["campaign_ids"] == ["cA"]
    assert rec["sibling_calls"] == 4 and rec["comparable"] == 4
    assert rec["dup_calls"] == 2 and abs(rec["dup_time_s"] - 60.0) < 1e-9     # 31 + 29, the first 30 s is not a dup
    assert rec["expensive_calls"] == 4            # 30, 31, 12, 29 s; the merge worker's read is not
    assert rec["turns"] == 5 and rec["workers"] == 5
    assert rec["final_source"] == "w4"
    # child 3's report says S03=300, the final says 999: that is a loss at the merge
    assert rec["graded"]["S03"] == "merge_loss" and rec["items_merge_loss"] == 1 and rec["items_ok"] == 2
    assert rec["dup_share_of_sibling_time"] == 60.0 / (30 + 31 + 12 + 29)


def test_a_row_with_the_same_worker_name_from_another_run_is_not_pulled_in(tmp_path):
    root, ledger = str(tmp_path), str(tmp_path / "ledger.jsonl")
    label = "01a_s01_r1_on"
    sd = _write_run(root, label, ledger)
    rows = [json.loads(x) for x in open(ledger, encoding="utf-8")]
    rows += _call(99, "w1", 50000.0, "shell_exec", "AAAA", 500.0)       # a later run, outside the window
    _jl(ledger, rows)
    run = {"label": label, "pair_id": "s01#1", "goal_id": "s01", "arm": "on", "state_dir": sd}
    rec = C.run_record(run, _goal(), _finished(root, label, "on"), ledger)
    assert rec["dup_calls"] == 2


def test_single_worker_run_has_no_campaign_and_final_comes_from_its_only_transcript(tmp_path):
    root, ledger = str(tmp_path), str(tmp_path / "ledger.jsonl")
    sd = os.path.join(root, "runs", "01b_s01_r1_off")
    _jl(os.path.join(sd, "transcripts", "rY_a0_w0.jsonl"),
        [{"meta": True, "key": "rY_a0_w0", "name": "w0", "goal": "g", "ts": 1000.0},
         {"turn": 1, "role": "assistant", "text": "S01=100\nS02=200\nS03=300"}])
    _jl(ledger, _call(1, "w0", 1010.0, "shell_exec", "AAAA", 40.0))
    run = {"label": "01b_s01_r1_off", "pair_id": "s01#1", "goal_id": "s01", "arm": "off", "state_dir": sd}
    rec = C.run_record(run, _goal(), _finished(root, "x", "off"), ledger)
    assert not rec["split"] and rec["dup_calls"] == 0
    assert rec["items_ok"] == 3 and rec["tool_calls"] == 1 and abs(rec["expensive_time_s"] - 40.0) < 1e-9


def test_collect_and_report_end_to_end(tmp_path, capsys):
    root = str(tmp_path)
    ledger = os.path.join(root, "ledger.jsonl")
    runs = C.make_plan(["s01"], 1, "exp-z", root)
    with open(os.path.join(root, "plan.json"), "w", encoding="utf-8") as fh:
        json.dump({"experiment": "exp-z", "runs": runs}, fh)
    os.makedirs(os.path.join(root, "data"))
    with open(os.path.join(root, "data", "goals.json"), "w", encoding="utf-8") as fh:
        json.dump([_goal()], fh)
    on = next(r for r in runs if r["arm"] == "on")
    off = next(r for r in runs if r["arm"] == "off")
    on_sd = _write_run(root, on["label"], ledger)
    assert on_sd == on["state_dir"]
    _jl(os.path.join(off["state_dir"], "transcripts", "rY_a0_w0.jsonl"),
        [{"meta": True, "key": "rY_a0_w0", "name": "w0", "goal": "g", "ts": 1000.0},
         {"turn": 1, "role": "assistant", "text": "S01=100\nS02=200\nS03=300"}])
    _jl(os.path.join(root, "runs.jsonl"),
        [_finished(root, on["label"], "on"), _finished(root, off["label"], "off")])
    assert C.main(["collect", "--root", root, "--ledger", ledger]) == 0
    assert os.path.exists(os.path.join(root, "analysis", "ab_manifest.json"))
    assert C.main(["report", "--root", root]) == 0
    text = open(os.path.join(root, "analysis", "report.md"), encoding="utf-8").read()
    assert "INSUFFICIENT" in text                      # one campaign: far under the 20 the K2 rule asks for
    assert "complete pairs: 1" in text and "where fan-out split" in text
