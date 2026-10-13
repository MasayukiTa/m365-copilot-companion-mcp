# -*- coding: utf-8 -*-
"""scripts/capability_profile.py on synthetic fleets, including one known-answer case."""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from scripts import capability_profile as CP  # noqa: E402


def _w(profile, outcome, turn, run="r1", i=0):
    return {"profile": profile, "name": "%s-%d" % (profile, i), "outcome": outcome,
            "turn": turn, "run_id": run, "task_id": "%s-t%d" % (profile, i)}


def _call(eid, tool, task=None, worker=None):
    return {"event": "call", "id": eid, "ts": 1.0, "tool": tool, "task": task, "worker": worker}


def test_sparse_group_is_unknown_not_judged():
    ws = [_w("p", "DONE", 3, i=i) for i in range(4)]
    rep = CP.derive([], ws, [])
    p = rep["profiles"]["p"]
    assert p["n_runs"] == 4
    assert p["success"]["label"] == "unknown" and p["turns"]["label"] == "unknown"
    assert p["confidence"] == "unknown"
    assert p["success"]["rate"] == 1.0          # the number is still shown


def test_enough_data_gives_good_and_poor():
    ws = [_w("strong", "DONE", 4, i=i) for i in range(5)]
    ws += [_w("weak", "DONE" if i == 0 else "FAILED", 30, i=i) for i in range(5)]
    rep = CP.derive([], ws, [])
    s, w = rep["profiles"]["strong"], rep["profiles"]["weak"]
    assert (s["success"]["label"], s["turns"]["label"]) == ("good", "good")
    assert (w["success"]["label"], w["turns"]["label"]) == ("poor", "poor")
    assert s["confidence"] == "low"
    big = CP.derive([], [_w("x", "DONE", 1, i=i) for i in range(20)], [])
    assert big["profiles"]["x"]["confidence"] == "adequate"


def test_min_n_is_configurable_and_running_workers_do_not_count():
    ws = [_w("p", "DONE", 2, i=i) for i in range(2)] + [_w("p", "", 9, i=9)]
    assert CP.derive([], ws, [])["profiles"]["p"]["n_runs"] == 2
    rep = CP.derive([], ws, [], min_n=2)
    assert rep["profiles"]["p"]["success"]["label"] == "good"


def test_empty_inputs():
    rep = CP.derive([], [], [])
    assert rep["profiles"] == {} and rep["unattributed_tool_calls"] == 0
    assert "no worker records" in CP.render_text(rep)


def test_missing_turn_is_not_zero_and_unnamed_group():
    ws = [{"outcome": "DONE"} for _ in range(5)]
    p = CP.derive([], ws, [])["profiles"][CP.UNNAMED]
    assert p["turns"] == {"mean": None, "n": 0, "label": "unknown"}
    assert p["success"]["label"] == "good"


def test_known_answer_by_hand():
    # group a: 6 runs, 5 DONE + 1 FAILED -> rate 5/6; turns 2,4,6,8,10,12 -> mean 7
    # group b: 5 runs, 2 DONE + 3 FAILED -> rate 0.4; turns all 20 -> mean 20
    ws = [_w("a", "FAILED" if i == 5 else "DONE", 2 * (i + 1), i=i) for i in range(6)]
    ws += [_w("b", "DONE" if i < 2 else "FAILED", 20, i=i) for i in range(5)]
    ev = [_call("1", "read_file", task="a-t0"), _call("2", "read_file", task="a-t1"),
          _call("3", "write_file", task="a-t2"), _call("4", "run", task="b-t0"),
          _call("5", "call_tool.describe", task="a-t0"),     # discovery: excluded
          _call("6", "read_file", task="nobody")]            # unattributable
    rep = CP.derive(ev, ws, [])
    a, b = rep["profiles"]["a"], rep["profiles"]["b"]
    assert abs(a["success"]["rate"] - 5 / 6) < 1e-9 and a["success"]["label"] == "good"
    assert a["turns"]["mean"] == 7 and a["turns"]["label"] == "good"
    assert b["success"]["rate"] == 0.4 and b["success"]["label"] == "poor"
    assert b["turns"]["mean"] == 20 and b["turns"]["label"] == "poor"
    assert a["tool_calls"] == 3
    assert a["tool_mix"]["read_file"] == {"calls": 2, "share": 2 / 3}
    assert b["tool_mix"] == {"run": {"calls": 1, "share": 1.0}}
    assert rep["unattributed_tool_calls"] == 1


def test_main_is_read_only_and_handles_empty_dir(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert CP.main(["--fleet-dir", str(empty)]) == 0
    out = capsys.readouterr().out
    head, _, tail = out.partition("\n# ")
    assert json.loads(head)["profiles"] == {} and "no worker records" in tail
    assert list(empty.iterdir()) == []

    fleet = tmp_path / "fleet"
    fleet.mkdir()
    ws = [_w("p", "DONE", 3, i=i) for i in range(5)]
    (fleet / "status.json").write_text(json.dumps({"workers": ws}), "utf-8")
    (fleet / "tool_events.jsonl").write_text(
        json.dumps(_call("1", "read_file", task="p-t0")) + "\n", "utf-8")
    before = {f.name: f.read_bytes() for f in fleet.iterdir()}
    assert CP.main(["--fleet-dir", str(fleet), "--format", "text"]) == 0
    assert "good" in capsys.readouterr().out
    assert before == {f.name: f.read_bytes() for f in fleet.iterdir()}


def test_missing_fleet_dir_does_not_raise(tmp_path):
    assert CP.main(["--fleet-dir", str(tmp_path / "nope"), "--format", "json"]) == 0
