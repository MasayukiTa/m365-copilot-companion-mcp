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


# ------------------------------------------------------------------ history-backed profile
# All of these use a temporary directory. None touches the live .fleet.

#: (run_id, outcome, turn) -- every run used the SAME slot name w0, as the real queue does.
RUNS = [("r1_a0", "DONE", 2), ("r2_a0", "DONE", 2), ("r3_a0", "DONE", 4),
        ("r4_a0", "DONE", 4), ("r5_a0", "STUCK", 6), ("r6_a0", "FANOUT", 6)]


def _hist(runs=RUNS, name="w0"):
    return [{"key": "%d#%s" % (i, name), "run_id": r, "name": name, "jid": "", "outcome": o,
             "turn": t, "status": "done", "goal": "g"} for i, (r, o, t) in enumerate(runs)]


def _effort_row(run, level, event="turn", instance="w0"):
    extra = {"event": event, "mode": "shadow"}
    extra["level" if event == "turn" else "run_level"] = level
    return {"mechanism": "effort_policy", "run_id": run, "instance": instance, "extra": extra}


#: r1-r3 stay auto; r4 starts auto and ends ultra; r5 ends ultra; r6 has no turn row at all.
MECH = [_effort_row("r1_a0", "custom", event="initial"),
        _effort_row("r1_a0", "auto"), _effort_row("r2_a0", "auto"), _effort_row("r3_a0", "auto"),
        _effort_row("r4_a0", "auto"), _effort_row("r4_a0", "ultra"), _effort_row("r5_a0", "ultra"),
        {"mechanism": "fanout", "run_id": "r6_a0", "instance": "", "extra": None}]


def _eff(mech=MECH):
    return CP.effort_reached_by_run(mech)


CAMP = [
    {"kind": "child_result", "campaign_id": "c1", "subtask_index": 0, "outcome": "DONE"},
    {"kind": "child_result", "campaign_id": "c1", "subtask_index": 1, "outcome": "MISSING"},
    {"kind": "child_result", "campaign_id": "c1", "subtask_index": 1, "outcome": "DONE"},
    {"kind": "child_result", "campaign_id": "c1", "subtask_index": 2, "outcome": "DONE"},
    {"kind": "child_result", "campaign_id": "c2", "subtask_index": 0, "outcome": "DONE"},
    {"kind": "child_result", "campaign_id": "c2", "subtask_index": 1, "outcome": "MISSING",
     "task_id": "tm"},
    {"kind": "merge_abandoned", "campaign_id": "c1"}, {"kind": "merge_requeued",
                                                        "campaign_id": "c1"},
    {"kind": "merge_done", "campaign_id": "c1"},
    {"kind": "merge_abandoned", "campaign_id": "c2"},
    {"kind": "merge_requeued", "campaign_id": "c3"},        # a retry is not a verdict
    {"kind": "campaign", "campaign_id": "c1", "n": 3}, {"kind": "merged", "campaign_id": "c1"},
]


def test_history_is_aggregated_per_role_not_per_slot():
    rep = CP.derive([], [], [], history=_hist(), effort_by_run=_eff())
    assert list(rep["profiles"]) == ["producer"]           # not "w0"
    p = rep["profiles"]["producer"]
    assert p["n_runs"] == 6
    assert abs(p["success"]["rate"] - 5 / 6) < 1e-9 and p["success"]["label"] == "good"
    assert p["turns"] == {"mean": 4.0, "n": 6, "label": "good"}
    assert p["outcomes"] == {"DONE": 4, "FANOUT": 1, "STUCK": 1}
    assert p["confidence"] == "low"
    assert rep["sources"] == {CP.SRC_HISTORY: 6}


def test_effort_reached_is_reported_but_is_not_the_identity():
    rep = CP.derive([], [], [], history=_hist(), effort_by_run=_eff())
    e = rep["profiles"]["producer"]["by_effort_reached"]
    assert set(e) == {"auto", "ultra", CP.NO_EFFORT}          # the "initial" row is not a level
    assert e["auto"] == {"n": 3, "success_rate": 1.0, "mean_turns": 8 / 3}
    assert e["ultra"] == {"n": 2, "success_rate": 0.5, "mean_turns": 5.0}
    assert e[CP.NO_EFFORT] == {"n": 1, "success_rate": 1.0, "mean_turns": 6.0}


def test_campaign_ledger_gives_subtask_and_aggregator_profiles():
    rep = CP.derive([], [], CAMP)
    sub, agg = rep["profiles"]["subtask"], rep["profiles"]["aggregator"]
    # (c1,1) appears twice: the last row (DONE) wins. 5 distinct children, 4 DONE, 1 MISSING.
    assert sub["n_runs"] == 5 and sub["outcomes"] == {"DONE": 4, "MISSING": 1}
    assert sub["success"]["rate"] == 0.8 and sub["success"]["label"] == "good"
    assert sub["turns"] == {"mean": None, "n": 0, "label": "unknown"}   # the ledger has no turns
    # c1 merged after an abandon + requeue -> DONE; c2 abandoned; c3 only requeued -> no verdict.
    assert agg["n_runs"] == 2 and agg["outcomes"] == {"ABANDONED": 1, "DONE": 1}
    assert agg["success"]["label"] == "unknown"                 # 2 < min_n
    assert "by_effort_reached" not in sub and "by_effort_reached" not in agg


def test_a_run_in_status_and_history_is_counted_once_and_status_wins():
    live = {"name": "w0", "run_id": "r6_a0", "role": "producer", "outcome": "STUCK", "turn": 6}
    rep = CP.derive([], [live], [], history=_hist())
    p = rep["profiles"]["producer"]
    assert p["n_runs"] == 6
    assert p["outcomes"] == {"DONE": 4, "STUCK": 2}        # r6 is the status row (STUCK), not FANOUT
    assert rep["sources"] == {CP.SRC_HISTORY: 5, CP.SRC_STATUS: 1}


def test_a_child_known_to_status_is_not_counted_again_from_the_ledger():
    live = {"name": "w3", "role": "subtask", "campaign_id": "c2", "task_id": "tm",
            "outcome": "DONE"}
    rep = CP.derive([], [live], CAMP)
    sub = rep["profiles"]["subtask"]
    assert sub["n_runs"] == 5 and sub["outcomes"] == {"DONE": 5}      # c2/1 came from status


def test_explicit_profile_field_still_wins_over_role():
    ws = [{"profile": "specialist", "role": "producer", "outcome": "DONE"}]
    assert list(CP.derive([], ws, [])["profiles"]) == ["specialist"]


def test_tool_calls_follow_role_or_ids_never_the_slot_name():
    hist = _hist()
    hist[0]["jid"] = "j1"
    ev = [_call("1", "read_file", task="j1", worker="w0"),         # by jid -> producer
          _call("2", "read_file", task="r2_a0", worker="w0"),      # by (run_id, name) -> producer
          dict(_call("3", "merge_tool"), role="aggregator"),       # by the role on the row
          _call("4", "read_file", worker="w0"),                    # slot name alone: unattributed
          _call("5", "read_file", task="r2_a0", worker="w9")]      # wrong slot for that run
    rep = CP.derive(ev, [], CAMP, history=hist)
    assert rep["profiles"]["producer"]["tool_calls"] == 2
    assert rep["profiles"]["aggregator"]["tool_mix"] == {"merge_tool": {"calls": 1, "share": 1.0}}
    assert rep["unattributed_tool_calls"] == 2


def _write_fleet(d, history=None, mech=None, camp=None, status=None, events=None):
    d.mkdir(parents=True, exist_ok=True)
    if history is not None:
        (d / "history.json").write_text(json.dumps(history), "utf-8")
    for name, rows in (("mechanisms.jsonl", mech), ("campaigns.jsonl", camp),
                       ("tool_events.jsonl", events)):
        if rows is not None:
            (d / name).write_text("".join(json.dumps(r) + "\n" for r in rows), "utf-8")
    if status is not None:
        (d / "status.json").write_text(json.dumps({"workers": status}), "utf-8")
    return d


def _snapshot(d):
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def test_each_source_is_read_from_disk_and_nothing_is_written(tmp_path, capsys):
    fleet = _write_fleet(tmp_path / "fleet", history=_hist(), mech=MECH, camp=CAMP,
                         status=[{"name": "w0", "run_id": "r9_a0", "role": "producer",
                                  "outcome": "DONE", "turn": 3}],
                         events=[_call("1", "read_file", task="r2_a0", worker="w0")])
    (fleet / "transcripts").mkdir()
    (fleet / "transcripts" / "t.jsonl").write_text("{}", "utf-8")
    before = _snapshot(fleet)
    assert CP.main(["--fleet-dir", str(fleet), "--format", "json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert _snapshot(fleet) == before                        # byte-identical, no new file
    assert rep["sources"] == {CP.SRC_CHILD: 5, CP.SRC_HISTORY: 6, CP.SRC_MERGE: 2,
                              CP.SRC_STATUS: 1}
    assert "unreadable" not in rep
    prod = rep["profiles"]["producer"]
    assert prod["n_runs"] == 7 and prod["tool_calls"] == 1
    assert prod["by_effort_reached"]["ultra"]["n"] == 2       # mechanisms.jsonl was read
    assert rep["profiles"]["subtask"]["n_runs"] == 5          # campaigns.jsonl was read
    text_rc = CP.main(["--fleet-dir", str(fleet), "--format", "text"])
    out = capsys.readouterr().out
    assert text_rc == 0 and "effort reached ultra" in out and "SKIPPED" not in out


def test_corrupted_and_partial_records_are_skipped_and_counted(tmp_path, capsys):
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    (fleet / "history.json").write_text(
        json.dumps(_hist()[:2] + ["garbage", 3, None]), "utf-8")        # 3 non-objects
    good = json.dumps({"kind": "child_result", "campaign_id": "c1", "subtask_index": 0,
                       "outcome": "DONE"})
    (fleet / "campaigns.jsonl").write_text(
        good + "\n" + '{"kind": "child_res' + "\n" + "[1, 2]\n" + "\n", "utf-8")   # cut + array
    (fleet / "mechanisms.jsonl").write_text("not json\n", "utf-8")
    before = _snapshot(fleet)
    assert CP.main(["--fleet-dir", str(fleet), "--format", "both"]) == 0
    out = capsys.readouterr().out
    head, _, tail = out.partition("\n# ")
    rep = json.loads(head)
    assert rep["unreadable"] == {"campaigns.jsonl": 2, "history.json": 3, "mechanisms.jsonl": 1}
    assert rep["profiles"]["producer"]["n_runs"] == 2         # the readable rest still counts
    assert rep["profiles"]["subtask"]["n_runs"] == 1
    assert "SKIPPED UNREADABLE RECORDS: campaigns.jsonl 2, history.json 3" in tail
    assert _snapshot(fleet) == before


def test_a_truncated_history_file_is_reported_not_raised(tmp_path, capsys):
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    (fleet / "history.json").write_text(json.dumps(_hist())[:-40], "utf-8")   # cut mid-write
    assert CP.main(["--fleet-dir", str(fleet), "--format", "json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["unreadable"] == {"history.json": 1} and rep["profiles"] == {}
    (fleet / "history.json").write_text('{"not": "a list"}', "utf-8")
    assert CP.load_all(str(fleet))[-1] == {"history.json": 1}
