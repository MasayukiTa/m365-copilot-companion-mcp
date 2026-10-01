# -*- coding: utf-8 -*-
"""Fan-out identity flows turn window -> tool ledger row -> sibling duplication report.

The job id and the campaign task ids are different id spaces; the coordinator writes both on each
turn row, the ledger copies the campaign side onto the call row, and the report joins on it.
Synthetic windows only (wall-clock epoch seconds).
"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import tool_ledger as L  # noqa: E402
from tools import turn_context as TC  # noqa: E402
from scripts import sibling_dup_report as R  # noqa: E402


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "LEDGER_PATH", str(tmp_path / "tool_events.jsonl"), raising=False)
    monkeypatch.setattr(TC, "CONTEXT_PATH", str(tmp_path / "turn_context.jsonl"), raising=False)
    L._SESSION_IDENTITY.clear()
    L._WINDOW_BINDING.clear()
    TC._CACHE.update(key=None, path=None, windows=[])
    return tmp_path


def _rows(path):
    return [json.loads(x) for x in open(path, encoding="utf-8").read().splitlines() if x.strip()]


def _ident(cid, i, role="subtask"):
    return {"campaign_id": cid, "task_id": "%s-%d" % (cid, i), "parent_task_id": "p1",
            "root_id": cid, "role": role}


def _call(monkeypatch, ts, session, args):
    monkeypatch.setattr(L, "session_fingerprint", lambda: session)
    L.record_call("read_file", args, ts=ts)
    return _rows(L.LEDGER_PATH)[-1]


def test_window_row_carries_identity_and_old_keys_are_unchanged(monkeypatch):
    TC.record_open("w1", "job1", "run1", 1, 100.0, ident=_ident("c1", 1))
    TC.record_close("w1", "job1", "run1", 1, 100.0, 110.0, ident=_ident("c1", 1))
    r = _call(monkeypatch, 105.0, "s1", {"path": "a"})
    assert (r["task"], r["worker"], r["attr"]) == ("job1", "w1", "window")
    assert (r["campaign_id"], r["subtask_id"], r["parent_task_id"], r["root_id"], r["role"]) \
        == ("c1", "c1-1", "p1", "c1", "subtask")
    # golden: without identity the row has exactly the pre-existing key set
    TC.record_open("w2", "job2", "run1", 1, 200.0)
    TC.record_close("w2", "job2", "run1", 1, 200.0, 210.0)
    plain = _call(monkeypatch, 205.0, "s2", {"path": "a"})
    assert sorted(plain) == sorted(["schema", "event", "id", "ts", "mono", "proc", "tool", "task",
                                    "worker", "turn", "args", "attr", "session"])


def test_old_rows_without_identity_still_load(monkeypatch):
    with open(TC.CONTEXT_PATH, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "open", "worker": "w1", "job": "job1", "run": "r",
                             "turn": 1, "t_send": 100.0}) + "\n")
        fh.write(json.dumps({"event": "close", "worker": "w1", "job": "job1", "run": "r",
                             "turn": 1, "t_send": 100.0, "t_done": 110.0}) + "\n")
    r = _call(monkeypatch, 105.0, "s1", {"path": "a"})
    assert r["task"] == "job1" and "campaign_id" not in r and "subtask_id" not in r


def test_overlapping_windows_stay_ambiguous_with_no_identity(monkeypatch):
    TC.record_open("w1", "job1", "r", 1, 100.0, ident=_ident("c1", 1))
    TC.record_open("w2", "job2", "r", 1, 105.0, ident=_ident("c1", 2))
    TC.record_close("w1", "job1", "r", 1, 100.0, 120.0, ident=_ident("c1", 1))
    TC.record_close("w2", "job2", "r", 1, 105.0, 125.0, ident=_ident("c1", 2))
    r = _call(monkeypatch, 110.0, "s1", {"path": "a"})
    assert r["attr"] == "ambiguous" and r["task"] == "" and r["worker"] == ""
    assert "campaign_id" not in r and "subtask_id" not in r


def test_two_siblings_sharing_a_campaign_produce_a_duplicate(monkeypatch):
    for i, (w, t0) in enumerate((("w1", 100.0), ("w2", 200.0)), start=1):
        TC.record_open(w, "job%d" % i, "run1", 1, t0, ident=_ident("c1", i))
        TC.record_close(w, "job%d" % i, "run1", 1, t0, t0 + 10, ident=_ident("c1", i))
    _call(monkeypatch, 105.0, "s1", {"path": "a"})
    _call(monkeypatch, 205.0, "s2", {"path": "a"})
    events = _rows(L.LEDGER_PATH)
    # make the args comparable the way the real ledger does (digests)
    for e in events:
        e["args"] = {"path": {"text": "a", "len": 1, "sha16": "ha"}}
    # the status roster uses the campaign ids; job ids appear nowhere in it
    workers = [{"campaign_id": "c1", "role": "subtask", "task_id": "c1-%d" % i,
                "outcome": "DONE", "name": "w%d" % i, "run_id": "other"} for i in (1, 2)]
    res = R.analyse(events, workers, [])
    s = R.summarise(res)
    assert res["buckets"]["sibling_calls"] == 2 and res["buckets"]["attributed_not_fanout"] == 0
    assert s["comparable"] == 2 and s["dup"] == 1 and s["m4"] == 1
    assert R.verdict(s).startswith("INSUFFICIENT (<")


def test_report_falls_back_to_the_status_join_for_old_rows():
    ev = []
    for i, (task, ts) in enumerate((("c1-1", 1.0), ("c1-2", 2.0))):
        ev.append({"event": "call", "id": str(i), "ts": ts, "tool": "read_file", "task": task,
                   "worker": "w", "attr": "window",
                   "args": {"path": {"text": "a", "len": 1, "sha16": "ha"}}})
    workers = [{"campaign_id": "c1", "role": "subtask", "task_id": "c1-%d" % i,
                "outcome": "DONE", "name": "w", "run_id": "r"} for i in (1, 2)]
    s = R.summarise(R.analyse(ev, workers, []))
    assert s["dup"] == 1


def test_a_merge_worker_row_is_not_a_sibling_call():
    ev = [{"event": "call", "id": "1", "ts": 1.0, "tool": "read_file", "task": "j", "worker": "w",
           "attr": "window", "campaign_id": "c1", "subtask_id": "c1-m", "role": "aggregator",
           "args": {"path": {"sha16": "ha"}}}]
    res = R.analyse(ev, [], [])
    assert res["buckets"]["sibling_calls"] == 0 and res["buckets"]["attributed_not_fanout"] == 1


def test_row_size_cap_is_unchanged():
    assert TC.WRITE_MAX_BYTES == 8 * 1024 * 1024 and TC.READ_TAIL_BYTES == 1024 * 1024
    long = {"campaign_id": "x" * 500, "role": "subtask"}
    TC.record_open("w1", "j", "r", 1, 1.0, ident=long)
    assert len(_rows(TC.CONTEXT_PATH)[0]["campaign_id"]) == 120
