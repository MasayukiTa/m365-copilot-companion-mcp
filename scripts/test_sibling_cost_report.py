# -*- coding: utf-8 -*-
"""scripts/sibling_cost_report.py on synthetic fleets: cost-weighted duplication, expensive
confinement, the attribution INSUFFICIENT path, thresholds, and read-only main."""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from scripts import sibling_cost_report as C  # noqa: E402


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
    res = C.analyse(events, workers, [])
    return res, C.summarise(res)


def test_duplicate_cost_is_weighted_by_the_duplicate_call_duration():
    # two siblings read the same path; the SECOND (12s) is the duplicate, so dup cost = 12, not 3.
    ev = []
    ev += _call(1, "read_file", 100, "c1-1", _args(path="a"), dur=3)
    ev += _call(2, "read_file", 101, "c1-2", _args(path="a"), dur=12)   # duplicate issuer
    ev += _call(3, "read_file", 102, "c1-2", _args(path="b"), dur=1)
    _, s = _run(ev, _workers("c1", ["DONE", "DONE"], merge="DONE"))
    assert s["dup"] == 1
    assert s["dup_cost_s"] == 12
    assert s["sib_cost_s"] == 16
    assert abs(s["dup_cost_share"] - 12 / 16) < 1e-9
    assert s["m4"] == 1


def test_expensive_confinement_counts_only_costly_tools():
    # a cheap duplicate (read_file) and an expensive duplicate (web_search); C2 sees only the latter.
    ev = []
    ev += _call(1, "read_file", 1, "c1-1", _args(p=1), dur=1)
    ev += _call(2, "read_file", 2, "c1-2", _args(p=1), dur=1)        # cheap duplicate
    ev += _call(3, "web_search", 3, "c1-1", _args(q="x"), dur=5)
    ev += _call(4, "web_search", 4, "c1-2", _args(q="x"), dur=7)     # expensive duplicate
    _, s = _run(ev, _workers("c1", ["DONE", "DONE"], merge="DONE"))
    assert s["dup"] == 2 and s["dup_cost_s"] == 8
    assert s["exp_dup"] == 1 and s["exp_dup_cost_s"] == 7
    assert s["exp_sib_cost_s"] == 12          # both web_search calls, 5 + 7
    assert abs(s["exp_dup_share"] - 7 / 12) < 1e-9


def test_a_tool_is_expensive_when_its_mean_duration_crosses_the_bar():
    # 'slow' is not in EXPENSIVE_TOOLS but its mean (3.0s) >= EXPENSIVE_MEAN_S, so it is expensive.
    ev = []
    ev += _call(1, "slow", 1, "c1-1", _args(p=1), dur=3)
    ev += _call(2, "slow", 2, "c1-2", _args(p=1), dur=3)             # duplicate, expensive by mean
    _, s = _run(ev, _workers("c1", ["DONE", "DONE"], merge="DONE"))
    assert s["exp_dup"] == 1 and s["exp_dup_cost_s"] == 3


def test_same_child_repeating_itself_is_not_duplication():
    ev = _call(1, "web_search", 1, "c1-1", _args(q=1), dur=5) \
        + _call(2, "web_search", 2, "c1-1", _args(q=1), dur=5)
    _, s = _run(ev, _workers("c1", ["DONE", "DONE"]))
    assert s["dup"] == 0 and s["dup_cost_s"] == 0


def test_a_duplicate_without_a_duration_adds_zero_cost_but_is_counted():
    ev = [{"event": "call", "id": "1", "ts": 1, "tool": "web_search", "task": "c1-1",
           "worker": "w0", "attr": "window", "args": _args(q=1)},
          {"event": "outcome", "id": "1", "ts": 2, "ok": True, "dur_mono_s": 5}]
    ev += [{"event": "call", "id": "2", "ts": 2, "tool": "web_search", "task": "c1-2",
            "worker": "w1", "attr": "window", "args": _args(q=1)}]  # duplicate, no outcome row
    _, s = _run(ev, _workers("c1", ["DONE", "DONE"], merge="DONE"))
    assert s["dup"] == 1 and s["dup_untimed"] == 1 and s["dup_cost_s"] == 0


def test_unattributed_and_discovery_never_count():
    ev = []
    ev += _call(1, "web_search", 1, "c1-1", _args(q=1), dur=5)
    ev += _call(2, "web_search", 2, "", _args(q=1), dur=5, attr=None, worker="")  # no attribution
    ev += _call(3, "call_tool.catalogue", 3, "c1-2", _args(q=1), dur=5)            # chatter
    res, s = _run(ev, _workers("c1", ["DONE", "DONE"]))
    b = res["buckets"]
    assert b["unknown_attribution"] == 1 and b["discovery_excluded"] == 1
    assert s["dup"] == 0


def _synthetic(n, dup_tool, dup_dur):
    ev, ws = [], []
    i = 0
    for k in range(n):
        cid = "k%d" % k
        ws += _workers(cid, ["DONE", "DONE"], merge="DONE")
        t = 1000.0 * (k + 1)
        i += 1
        ev += _call(i, dup_tool, t, cid + "-1", _args(p=k), dur=dup_dur)
        i += 1
        ev += _call(i, dup_tool, t + 1, cid + "-2", _args(p=k), dur=dup_dur)   # duplicate
        i += 1
        ev += _call(i, dup_tool, t + 2, cid + "-1", _args(p=k, extra=1), dur=dup_dur)
    return ev, ws


def test_verdict_low_and_notable_on_expensive_share():
    # expensive tool, every campaign duplicates one of three expensive calls => 1/3 share => NOTABLE
    ev, ws = _synthetic(20, "web_search", dup_dur=5)
    _, s = _run(ev, ws)
    assert s["m4"] == 20 and C.verdict(s) == "NOTABLE"
    # cheap tool: no expensive sibling cost at all => INSUFFICIENT (no expensive cost), not LOW
    ev, ws = _synthetic(20, "read_file", dup_dur=0.01)
    _, s = _run(ev, ws)
    assert s["m4"] == 20 and C.verdict(s).startswith("INSUFFICIENT")


def test_attribution_insufficient_names_the_schema_gap():
    # live-shaped rows: empty task, tab-local worker, no campaign_id -> nothing maps.
    ev = _call(1, "web_search", 1, "", _args(q=1), dur=5, worker="w51", attr="explicit")
    ev += _call(2, "web_search", 2, "", _args(q=1), dur=5, worker="w69", attr="explicit")
    res, s = _run(ev, _workers("cX", ["DONE", "DONE"], merge="DONE"))
    assert res["buckets"]["sibling_calls"] == 0
    assert s["sibling_calls"] == 0
    v = C.verdict(s)
    assert v.startswith("INSUFFICIENT (attribution")
    assert "campaign_id/subtask_id" in v


def test_fewer_than_twenty_campaigns_is_insufficient():
    ev = _call(1, "web_search", 1, "c1-1", _args(q=1), dur=5)
    _, s = _run(ev, _workers("c1", ["DONE", "DONE"], merge="DONE"))
    assert C.verdict(s) == "INSUFFICIENT (<20 qualifying campaigns)"


def test_threshold_constants_match_the_stated_rule():
    assert C.MIN_CAMPAIGNS == 20
    assert C.EXPENSIVE_DUP_SHARE_LIMIT == 0.05
    assert C.EXPENSIVE_MEAN_S == 2.0
    assert "web_search" in C.EXPENSIVE_TOOLS and "web_fetch" in C.EXPENSIVE_TOOLS


def test_main_reads_a_fleet_dir_read_only(tmp_path, capsys):
    ev, ws = _synthetic(2, "web_search", dup_dur=5)
    with open(tmp_path / "tool_events.jsonl", "w", encoding="utf-8") as fh:
        for r in ev:
            fh.write(json.dumps(r) + "\n")
    with open(tmp_path / "status.json", "w", encoding="utf-8") as fh:
        json.dump({"workers": ws}, fh)
    before = sorted(os.listdir(tmp_path))
    assert C.main(["--fleet-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "INSUFFICIENT" in out and "C2 EXPENSIVE duplicate cost" in out
    assert sorted(os.listdir(tmp_path)) == before


def test_missing_fleet_dir_reports_insufficient_without_raising(tmp_path, capsys):
    assert C.main(["--fleet-dir", str(tmp_path / "absent")]) == 0
    assert "INSUFFICIENT" in capsys.readouterr().out
