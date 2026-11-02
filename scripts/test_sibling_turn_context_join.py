# -*- coding: utf-8 -*-
"""Turn-window attribution for the sibling reports.

The live MCP session cannot label a fan-out worker's tool calls (empty task, no campaign_id), so
without this join the B3 reports see zero sibling calls. These tests prove that joining a call's
(worker, wall-clock ts) to the coordinator's turn windows recovers the campaign child -- over data
already on disk, with no new run -- and that the join is additive: a call the existing task/worker
join already places is untouched, an absent turn_context.jsonl changes nothing, and only a
subtask-role window with both ids attributes a call.
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import turn_context_join as J  # noqa: E402
from scripts import sibling_dup_report as R  # noqa: E402
from scripts import sibling_cost_report as C  # noqa: E402


def _args(**kw):
    return {k: {"text": str(v), "len": len(str(v)), "sha16": "h" + str(v)} for k, v in kw.items()}


def _call(i, tool, ts, worker, args, dur=1.0):
    """A ledger call the MCP session COULD NOT label: empty task, no campaign_id, a worker name."""
    c = {"event": "call", "id": str(i), "ts": ts, "tool": tool, "task": "", "worker": worker,
         "attr": "window", "args": args}
    o = {"event": "outcome", "id": str(i), "ts": ts + dur, "ok": True, "dur_mono_s": dur}
    return [c, o]


def _open(worker, cid, child, t_send, role="subtask"):
    return {"event": "open", "worker": worker, "job": "", "run": "r" + cid, "turn": 1,
            "t_send": t_send, "campaign_id": cid, "task_id": child, "role": role, "ts": t_send}


def _close(worker, cid, child, t_send, t_done, role="subtask"):
    return {"event": "close", "worker": worker, "job": "", "run": "r" + cid, "turn": 1,
            "t_send": t_send, "t_done": t_done, "campaign_id": cid, "task_id": child,
            "role": role, "ts": t_done}


def _windows(rows):
    return J.WindowIndex(J.build_windows(rows))


def _workers(cid, outcomes, merge=None):
    ws = [{"campaign_id": cid, "role": "subtask", "task_id": "%s-%d" % (cid, i + 1),
           "jid": "j%s%d" % (cid, i), "outcome": o, "name": "w%d" % i, "run_id": "r" + cid}
          for i, o in enumerate(outcomes)]
    if merge:
        ws.append({"campaign_id": cid, "role": "aggregator", "task_id": cid + "-m",
                   "outcome": merge, "name": "m", "run_id": "r" + cid})
    return ws


# -- WindowIndex / build_windows -------------------------------------------------------------

def test_a_single_matching_window_gives_its_identity():
    w = _windows([_open("w31", "cA", "cA-2", 100.0), _close("w31", "cA", "cA-2", 100.0, 200.0)])
    assert w.identity_of("w31", 150.0) == {"campaign_id": "cA", "task_id": "cA-2", "role": "subtask"}


def test_slack_extends_both_edges():
    w = _windows([_open("w31", "cA", "cA-2", 100.0), _close("w31", "cA", "cA-2", 100.0, 200.0)])
    assert w.identity_of("w31", 99.0)["campaign_id"] == "cA"     # 1.0s before send, within 1.5 slack
    assert w.identity_of("w31", 201.0)["campaign_id"] == "cA"    # 1.0s after done
    assert w.identity_of("w31", 97.0) == {}                      # beyond slack


def test_no_match_and_wrong_worker_give_empty():
    w = _windows([_open("w31", "cA", "cA-2", 100.0), _close("w31", "cA", "cA-2", 100.0, 200.0)])
    assert w.identity_of("w31", 500.0) == {}
    assert w.identity_of("w99", 150.0) == {}


def test_disagreeing_windows_of_one_worker_are_ambiguous_not_guessed():
    # two touching turns of the SAME worker, different children: the overlap at the seam is ambiguous
    rows = [_open("w31", "cA", "cA-1", 100.0), _close("w31", "cA", "cA-1", 100.0, 200.0),
            _open("w31", "cA", "cA-2", 200.5), _close("w31", "cA", "cA-2", 200.5, 300.0)]
    w = _windows(rows)
    assert w.identity_of("w31", 200.2) == {}        # inside both (slack): refused
    assert w.identity_of("w31", 150.0)["task_id"] == "cA-1"
    assert w.identity_of("w31", 280.0)["task_id"] == "cA-2"


def test_an_open_without_a_close_is_bounded_but_usable():
    w = _windows([_open("w31", "cA", "cA-2", 100.0)])
    assert w.identity_of("w31", 150.0)["campaign_id"] == "cA"
    assert w.identity_of("w31", 100.0 + J.MAX_OPEN_S + 5.0) == {}   # past MAX_OPEN_S + slack


def test_a_window_without_identity_yields_nothing():
    rows = [{"event": "open", "worker": "w31", "t_send": 100.0},
            {"event": "close", "worker": "w31", "t_send": 100.0, "t_done": 200.0}]
    w = _windows(rows)
    assert w.identity_of("w31", 150.0) == {}
    assert w.windows == 1 and w.with_ident == 0


# -- Roster.resolve_by_window ----------------------------------------------------------------

def test_roster_window_join_only_attributes_a_subtask_window():
    wins = _windows([_open("w0", "cA", "cA-1", 100.0), _close("w0", "cA", "cA-1", 100.0, 200.0),
                     _open("wm", "cA", "cA-m", 100.0, role="aggregator"),
                     _close("wm", "cA", "cA-m", 100.0, 200.0, role="aggregator")])
    roster = R.Roster(_workers("cA", ["DONE", "DONE"], merge="DONE"), [], windows=wins)
    assert roster.resolve_by_window("w0", 150.0) == ("cA", "cA-1")
    assert roster.resolve_by_window("wm", 150.0) is None          # aggregator is not a sibling call
    assert roster.resolve_by_window("w0", 999.0) is None


def test_window_join_never_overrides_an_existing_task_join():
    # a row the task/worker join already resolves must not be re-decided by a window
    wins = _windows([_open("w0", "cA", "cA-2", 100.0), _close("w0", "cA", "cA-2", 100.0, 200.0)])
    roster = R.Roster(_workers("cA", ["DONE", "DONE"]), [], windows=wins)
    call = {"task": "jcA0", "worker": "w0", "ts": 150.0}         # jid of child cA-1
    assert roster.resolve_call(call) == ("cA", "cA-1")            # task join wins, not the window


# -- end to end through analyse --------------------------------------------------------------

def test_calls_with_no_task_are_recovered_by_the_window_and_counted_as_duplicates():
    cid = "cB"
    wins = _windows([
        _open("wa", cid, cid + "-1", 100.0), _close("wa", cid, cid + "-1", 100.0, 300.0),
        _open("wb", cid, cid + "-2", 100.0), _close("wb", cid, cid + "-2", 100.0, 300.0)])
    ev = []
    ev += _call(1, "web_search", 150.0, "wa", _args(q="x"), dur=5)
    ev += _call(2, "web_search", 160.0, "wb", _args(q="x"), dur=5)      # sibling duplicate
    ev += _call(3, "web_search", 170.0, "wb", _args(q="y"), dur=5)
    ws = _workers(cid, ["DONE", "DONE"], merge="DONE")
    # WITHOUT windows: nothing maps (empty task), the old INSUFFICIENT state
    res0 = R.analyse(ev, ws, [])
    assert res0["buckets"]["sibling_calls"] == 0
    assert res0["buckets"]["attributed_not_fanout"] == 3
    # WITH windows: all three map, one is a cross-sibling duplicate
    res1 = R.analyse(ev, ws, [], windows=wins)
    assert res1["buckets"]["sibling_calls"] == 3
    assert res1["buckets"]["window_attributed"] == 3
    s1 = R.summarise(res1)
    assert s1["dup"] == 1 and s1["comparable"] == 3


def test_cost_report_also_recovers_and_weights_by_expense():
    cid = "cC"
    wins = _windows([
        _open("wa", cid, cid + "-1", 100.0), _close("wa", cid, cid + "-1", 100.0, 300.0),
        _open("wb", cid, cid + "-2", 100.0), _close("wb", cid, cid + "-2", 100.0, 300.0)])
    ev = []
    ev += _call(1, "web_search", 150.0, "wa", _args(q="x"), dur=5)
    ev += _call(2, "web_search", 160.0, "wb", _args(q="x"), dur=7)      # expensive duplicate
    ws = _workers(cid, ["DONE", "DONE"], merge="DONE")
    res0 = C.analyse(ev, ws, [])
    assert res0["buckets"]["sibling_calls"] == 0
    res1 = C.analyse(ev, ws, [], windows=wins)
    assert res1["buckets"]["sibling_calls"] == 2
    assert res1["buckets"]["window_attributed"] == 2
    s1 = C.summarise(res1)
    assert s1["exp_dup"] == 1 and s1["exp_dup_cost_s"] == 7


# -- load_windows reads the file(s) read-only ------------------------------------------------

def test_load_windows_reads_jsonl_and_rotated_without_writing(tmp_path):
    with open(tmp_path / "turn_context.jsonl.1", "w", encoding="utf-8") as fh:
        fh.write(json.dumps(_open("w1", "cZ", "cZ-1", 10.0)) + "\n")
    with open(tmp_path / "turn_context.jsonl", "w", encoding="utf-8") as fh:
        fh.write(json.dumps(_close("w1", "cZ", "cZ-1", 10.0, 20.0)) + "\n")
    before = sorted(os.listdir(tmp_path))
    idx = J.load_windows(str(tmp_path))
    assert idx.identity_of("w1", 15.0) == {"campaign_id": "cZ", "task_id": "cZ-1", "role": "subtask"}
    assert sorted(os.listdir(tmp_path)) == before            # nothing written


def test_absent_turn_context_is_an_empty_index_that_changes_nothing(tmp_path):
    idx = J.load_windows(str(tmp_path))
    assert idx.windows == 0 and idx.identity_of("w1", 1.0) == {}
    # analyse with an empty index equals analyse with none
    ev = _call(1, "x", 1.0, "wa", _args(p=1))
    ws = _workers("cD", ["DONE", "DONE"])
    a = R.analyse(ev, ws, [], windows=idx)
    b = R.analyse(ev, ws, [])
    assert a["buckets"]["sibling_calls"] == b["buckets"]["sibling_calls"] == 0
