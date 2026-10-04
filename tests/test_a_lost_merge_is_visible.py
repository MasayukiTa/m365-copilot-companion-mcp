# -*- coding: utf-8 -*-
"""A merge is issued only when every slot is accounted for, and a merge lost twice is VISIBLE.

Two shapes seen in a live depth-2 fan-out run, reproduced with synthetic ledgers:

  A. a family of 7 with ONE child answer on the ledger. It must not merge on that; it merges
     only when every other slot has an explicit terminal MISSING row, and then the merge input
     NAMES those slots (and carries the acceptance checks that forbid claiming completeness).
  B. a merge queued, lost, re-issued once and lost again. The cap stays one; after it the family
     is written off ONCE (`merge_abandoned`), shown as merge_state 'failed', its parent slot is
     MISSING, a later start does not resurrect it, and ledgers without the line still load.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import family_view as fv  # noqa: E402
from relay import fanout, fleet_resume as fr  # noqa: E402
from relay import mechanism_telemetry as MT  # noqa: E402
import relay.relay_fleet as rf  # noqa: E402


def _seam():
    spec = importlib.util.spec_from_file_location(
        "seam_helpers_lost_merge",
        os.path.join(REPO, "relay", "test_the_recovered_family_reaches_a_merge.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _rows(tmp_path):
    p = tmp_path / "campaigns.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _kinds(tmp_path, cid):
    return [r.get("kind") for r in _rows(tmp_path) if r.get("campaign_id") == cid]


@pytest.fixture(autouse=True)
def _mech(tmp_path, monkeypatch):
    monkeypatch.setattr(MT, "LOG", str(tmp_path / "mech.jsonl"), raising=False)


def _mech_rows(tmp_path):
    p = tmp_path / "mech.jsonl"
    if not p.is_file():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


# ---- A. a short family is not merged ---------------------------------------------------------

def _result(h, i, outcome, text="x"):
    return {"kind": "child_result", "campaign_id": h.CID, "subtask_index": i,
            "outcome": outcome, "result": text}


def test_seven_slots_one_answer_is_not_merged(tmp_path, monkeypatch):
    h = _seam()
    rows = h._header(n=7) + [_result(h, 6, "DONE", "chapter six")]
    assert h._aggregators(h._run(tmp_path, monkeypatch, rows, [])) == []
    assert "merged" not in _kinds(tmp_path, h.CID)


def test_terminal_missing_slots_merge_and_are_named(tmp_path, monkeypatch):
    h = _seam()
    rows = h._header(n=7) + [_result(h, 6, "DONE", "chapter six")] \
        + [_result(h, i, "MISSING", "") for i in (1, 2, 3, 4, 5, 7)]
    res = h._run(tmp_path, monkeypatch, rows, [])
    aggs = h._aggregators(res)
    assert len(aggs) == 1
    text = aggs[0]["goal"]
    assert "未完了: 1, 2, 3, 4, 5, 7" in text and "未取得" in text
    assert "全サブタスクが完了しています" not in text


def test_a_still_queued_child_blocks_the_merge():
    recs = [{"finished": True, "outcome": "DONE", "subtask_index": 1, "result": "a"},
            {"finished": False, "outcome": "", "subtask_index": 2, "result": ""}]
    assert fanout.ready_to_aggregate(recs) is False


def test_terminal_stuck_children_are_named_by_the_merge_goal():
    recs = [{"finished": True, "outcome": "DONE", "subtask_index": 1, "result": "a"}] + [
        {"finished": True, "outcome": "STUCK", "subtask_index": i, "result": "b"}
        for i in range(2, 8)]
    assert fanout.ready_to_aggregate(recs)
    goal = fanout.aggregation_goal("parent", recs, campaign_id="c")
    assert "未完了: 2, 3, 4, 5, 6, 7" in goal["text"]
    assert any(c.get("needle") == "欠落なし" and c.get("expect") is False
               for c in goal["checks"])


# ---- B. a merge lost twice ---------------------------------------------------------------------

def _lost_twice(h, n=2):
    rows = h._header(n=n, merged=True)
    for r in rows:
        if r.get("kind") == "merged":
            r["agg_key"] = "AGGKEY"
    for i in range(1, n + 1):
        rows.append(_result(h, i, "DONE", "answer %d" % i))
    rows.append({"kind": "merge_requeued", "campaign_id": h.CID, "attempt": 1})
    return rows


def test_a_merge_lost_twice_is_written_off_once(tmp_path, monkeypatch):
    h = _seam()
    res = h._run(tmp_path, monkeypatch, _lost_twice(h), [])
    assert h._aggregators(res) == [], "the re-issue cap must not be raised"
    assert _kinds(tmp_path, h.CID).count("merge_abandoned") == 1
    assert [r for r in _mech_rows(tmp_path) if r.get("mechanism") == "merge_abandoned"]
    # a later start neither resurrects the family nor writes the line again
    tmp2 = tmp_path / "again"
    tmp2.mkdir()
    res2 = h._run(tmp2, monkeypatch, _rows(tmp_path), [])
    assert h._aggregators(res2) == []
    assert _kinds(tmp2, h.CID).count("merge_abandoned") == 1
    goals, _ = fr.resume_children_goals(str(tmp2), {}, log=lambda m: None, scope={h.CID})
    assert goals == []


def test_old_ledgers_without_the_marker_load_and_the_marker_is_not_a_child():
    lines = [json.dumps(r) for r in [
        {"kind": "campaign", "campaign_id": "c", "goal": "g", "n": 1},
        {"kind": "merged", "campaign_id": "c", "agg_key": "K"}]]
    fam = fanout.campaigns_from_ledger(lines)["c"]
    assert "merge_abandoned" not in fam
    lines.append(json.dumps({"kind": "merge_abandoned", "campaign_id": "c"}))
    fam = fanout.campaigns_from_ledger(lines)["c"]
    assert fam["merge_abandoned"] is True and fam["children"] == []


def _w(**o):
    base = dict(name="", campaign_id="", task_id="", parent_task_id=None, role="",
                subtask_index=None, status="done", outcome="", goal="", turn=0)
    base.update(o)
    return base


def _head(cid, ptid, pcid="", root=""):
    return json.dumps({"kind": "campaign", "campaign_id": cid, "goal": "g", "n": 2,
                       "parent_task_id": ptid, "parent_campaign_id": pcid,
                       "root_id": root or cid})


def _nested_fixture():
    ws = [_w(name="po", campaign_id="cO", task_id="tpo", role="producer", outcome="FANOUT"),
          _w(name="a", campaign_id="cO", task_id="tk0", parent_task_id="tpo", role="subtask",
             outcome="DONE"),
          _w(name="b", campaign_id="cO", task_id="tk1", parent_task_id="tpo", role="subtask",
             outcome="FANOUT")]
    ws += [_w(name="i%d" % i, campaign_id="cI", task_id="ti%d" % i, parent_task_id="tk1",
              role="subtask", outcome="DONE") for i in range(2)]
    return ws, [_head("cO", "tpo"), _head("cI", "tk1", pcid="cO", root="cO")]


def test_family_view_shows_failed_not_waiting():
    ws, lines = _nested_fixture()
    g = {x["group_id"]: x for x in fv.build_groups(ws, lines + [
        json.dumps({"kind": "merged", "campaign_id": "cI", "agg_key": "K"})])}
    assert g["cI"]["merge_state"] == "merging"          # queued, nothing visible: still 'waiting'
    lines.append(json.dumps({"kind": "merge_abandoned", "campaign_id": "cI"}))
    g = {x["group_id"]: x for x in fv.build_groups(ws, lines)}
    assert g["cI"]["merge_state"] == "failed"
    assert g["cI"]["parent"]["display_state"] in ("", "merge_failed")
    # the outer group no longer waits on a subgroup that will never merge
    assert g["cO"]["merge_state"] != "waiting_on_subgroups"


def test_a_nested_merge_lost_twice_marks_the_parent_slot_missing(tmp_path):
    rows = [
        {"kind": "campaign", "campaign_id": "cO", "goal": "outer", "n": 2},
        {"kind": "campaign", "campaign_id": "cI", "goal": "inner", "n": 2,
         "parent_campaign_id": "cO", "parent_subtask_index": 2},
        {"kind": "child_result", "campaign_id": "cO", "subtask_index": 1, "outcome": "DONE",
         "result": "one"},
        {"kind": "merged", "campaign_id": "cI", "agg_key": "K"},
        {"kind": "merge_requeued", "campaign_id": "cI", "attempt": 1},
    ]
    with io.open(str(tmp_path / "campaigns.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    t = tmp_path / "transcripts"
    t.mkdir()
    out = rf._campaigns_from_disk(str(t))
    assert "cI" not in out                              # still dropped, never re-issued
    slot = [r for r in _rows(tmp_path)
            if r.get("kind") == "child_result" and r.get("campaign_id") == "cO"
            and r.get("nested") == "cI"]
    assert len(slot) == 1 and slot[0]["outcome"] == "MISSING" and slot[0]["subtask_index"] == 2
    # the parent carried in memory sees the slot as a finished MISSING record
    recs = [fanout.slot_record(r["subtask_index"], r) for r in out["cO"]["child_results"]
            if r.get("nested")]
    assert recs and recs[0]["finished"] and recs[0]["outcome"] == "MISSING"
    # idempotent
    rf._campaigns_from_disk(str(t))
    assert len([r for r in _rows(tmp_path) if r.get("kind") == "merge_abandoned"]) == 1
    assert len([r for r in _rows(tmp_path) if r.get("nested") == "cI"]) == 1
