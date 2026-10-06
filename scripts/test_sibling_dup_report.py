# -*- coding: utf-8 -*-
"""scripts/sibling_dup_report.py on synthetic fleets: duplicates, none, unattributed, verdicts."""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from scripts import sibling_dup_report as R  # noqa: E402


def _args(**kw):
    return {k: {"text": str(v), "len": len(str(v)), "sha16": "h" + str(v)} for k, v in kw.items()}


def _call(i, tool, ts, task, args, dur=1.0, attr="window", worker="w0"):
    c = {"event": "call", "id": str(i), "ts": ts, "tool": tool, "task": task, "worker": worker,
         "attr": attr, "args": args}
    o = {"event": "outcome", "id": str(i), "ts": ts + dur, "ok": True, "dur_mono_s": dur}
    return [c, o]


def _workers(cid, outcomes, merge=None):
    ws = [{"campaign_id": cid, "role": "subtask", "task_id": "%s-%d" % (cid, i + 1),
           "jid": "j%s%d" % (cid, i), "outcome": o, "name": "w%d" % i, "run_id": "r" + cid}
          for i, o in enumerate(outcomes)]
    if merge:
        ws.append({"campaign_id": cid, "role": "aggregator", "task_id": cid + "-m",
                   "outcome": merge, "name": "m", "run_id": "r" + cid})
    return ws


def _run(events, workers):
    res = R.analyse(events, workers, [])
    return res, R.summarise(res)


def test_duplicates_across_siblings_are_counted_once_per_repeat():
    ev = []
    ev += _call(1, "read_file", 100, "c1-1", _args(path="a"), dur=2)
    ev += _call(2, "read_file", 101, "c1-2", _args(path="a"), dur=3)      # duplicate
    ev += _call(3, "read_file", 102, "c1-2", _args(path="b"), dur=1)
    res, s = _run(ev, _workers("c1", ["DONE", "DONE"], merge="DONE"))
    assert s["comparable"] == 3 and s["dup"] == 1
    assert abs(s["dup_rate"] - 1 / 3) < 1e-9
    assert s["dup_time_s"] == 3
    assert s["m4"] == 1


def test_same_child_repeating_itself_is_not_sibling_duplication():
    ev = _call(1, "x", 1, "c1-1", _args(p=1)) + _call(2, "x", 2, "c1-1", _args(p=1))
    _, s = _run(ev, _workers("c1", ["DONE", "DONE"]))
    assert s["dup"] == 0


def test_no_duplicates_gives_zero_rate():
    ev = _call(1, "x", 1, "c1-1", _args(p=1)) + _call(2, "x", 2, "c1-2", _args(p=2))
    _, s = _run(ev, _workers("c1", ["DONE", "DONE"]))
    assert s["dup"] == 0 and s["dup_rate"] == 0


def test_unattributed_rows_go_to_unknown_and_never_count_as_duplicates():
    ev = []
    ev += _call(1, "x", 1, "c1-1", _args(p=1))
    ev += _call(2, "x", 2, "", _args(p=1), attr=None, worker="")            # no attribution
    ev += _call(3, "x", 3, "c1-2", _args(p=1), attr="ambiguous")             # ambiguous
    ev += _call(4, "call_tool.catalogue", 4, "c1-2", _args(p=1))             # chatter
    ev += _call(5, "x", 5, "nobody", _args(p=1))                             # not a fan-out child
    res, s = _run(ev, _workers("c1", ["DONE", "DONE"]))
    b = res["buckets"]
    assert b["unknown_attribution"] == 2
    assert b["discovery_excluded"] == 1
    assert b["attributed_not_fanout"] == 1
    assert b["sibling_calls"] == 1
    assert s["dup"] == 0


def test_calls_without_args_are_excluded_from_the_rate_not_called_duplicates():
    ev = _call(1, "x", 1, "c1-1", None) + _call(2, "x", 2, "c1-2", None)
    res, s = _run(ev, _workers("c1", ["DONE", "DONE"]))
    assert res["buckets"]["no_args"] == 2
    assert s["comparable"] == 0 and s["dup_rate"] is None


def test_campaign_needs_two_completed_children_to_count():
    ev = _call(1, "x", 1, "c1-1", _args(p=1))
    _, s = _run(ev, _workers("c1", ["DONE", "FAIL"]))
    assert s["m4"] == 0 and s["with_2_completed"] == 0


def test_merge_with_missing_child_is_a_loss_attributable_to_children():
    ev = _call(1, "x", 1, "c2-1", _args(p=1))
    ws = _workers("c2", ["DONE", "DONE", "STUCK"], merge="DONE")
    _, s = _run(ev, ws)
    assert s["merges"] == 1 and s["merge_failed"] == 1 and s["merge_lost_info"] == 1


def test_fewer_than_twenty_campaigns_is_insufficient():
    ev = _call(1, "x", 1, "c1-1", _args(p=1))
    _, s = _run(ev, _workers("c1", ["DONE", "DONE"], merge="DONE"))
    assert R.verdict(s) == "INSUFFICIENT (<20 campaigns)"


def _synthetic(n, dup_dur, wall_extra, lost=0):
    ev, ws = [], []
    i = 0
    for k in range(n):
        cid = "k%d" % k
        outcomes = ["DONE", "DONE"] + (["STUCK"] if k < lost else [])
        ws += _workers(cid, outcomes, merge="DONE")
        t = 1000.0 * (k + 1)
        i += 1
        ev += _call(i, "x", t, cid + "-1", _args(p=k), dur=1)
        i += 1
        ev += _call(i, "x", t + 1, cid + "-2", _args(p=k), dur=dup_dur)   # duplicate
        i += 1
        ev += _call(i, "y", t + wall_extra, cid + "-1", _args(p=k), dur=1)
    return ev, ws


def test_thresholds_low_and_notable():
    ev, ws = _synthetic(20, dup_dur=1, wall_extra=100)           # ~1% duplicate time, no loss
    _, s = _run(ev, ws)
    assert s["m4"] == 20 and R.verdict(s) == "LOW"
    ev, ws = _synthetic(20, dup_dur=30, wall_extra=100)          # ~30% duplicate time
    _, s = _run(ev, ws)
    assert R.verdict(s) == "NOTABLE"
    ev, ws = _synthetic(20, dup_dur=1, wall_extra=100, lost=4)   # 4 of 20 merges lost info
    _, s = _run(ev, ws)
    assert R.verdict(s) == "NOTABLE"
    ev, ws = _synthetic(20, dup_dur=1, wall_extra=100, lost=3)   # 3/20 = 15% < 20% limit
    _, s = _run(ev, ws)
    assert R.verdict(s) == "LOW"


def test_threshold_constants_match_the_stated_rule():
    assert R.MIN_CAMPAIGNS == 20
    assert R.DUP_TIME_SHARE_LIMIT == 0.05
    assert R.MERGE_LOSS_LIMIT == 0.2


def test_main_reads_a_fleet_dir_read_only(tmp_path, capsys):
    ev, ws = _synthetic(2, dup_dur=1, wall_extra=10)
    with open(tmp_path / "tool_events.jsonl", "w", encoding="utf-8") as fh:
        for r in ev:
            fh.write(json.dumps(r) + "\n")
    with open(tmp_path / "status.json", "w", encoding="utf-8") as fh:
        json.dump({"workers": ws}, fh)
    before = sorted(os.listdir(tmp_path))
    assert R.main(["--fleet-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "INSUFFICIENT (<20 campaigns)" in out and "M1" in out
    assert sorted(os.listdir(tmp_path)) == before


def test_missing_fleet_dir_reports_insufficient_without_raising(tmp_path, capsys):
    assert R.main(["--fleet-dir", str(tmp_path / "absent")]) == 0
    assert "INSUFFICIENT" in capsys.readouterr().out
