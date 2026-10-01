# -*- coding: utf-8 -*-
"""A parent slot whose child split again is complete only when the nested merge is done.

THE DEFECT THIS GUARDS: a child that itself splits ends FANOUT, which counts as finished. The
parent's merge then counted that slot as done and took the child's split-proposal TEXT as the
slot's answer, and nothing tied the nested campaign's own merge back to the slot.

What is held here, all through run_relay_fleet with fake workers (no browser):
  * 2-level and 3-level families: every slot DONE -> each merge issued once, with the nested
    merge's ANSWER in the slot and no split-proposal text anywhere in a merge input;
  * a grandchild STUCK -> the parent slot is explicitly MISSING and the root merge names it;
  * a nested merge STUCK -> the same, never an empty success;
  * resume mid-tree: a finished nested merge is not issued again, the root merge happens once;
  * a budget refusal inside a nested level still runs that slice directly;
  * a flat (depth-1) family is byte-identical to what main produced (golden digest);
  * HIERARCHICAL_MERGE_READY is never set True by production code.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import fanout  # noqa: E402
from relay import fanout_budget as fb  # noqa: E402
from relay import fleet_resume as fr  # noqa: E402
import relay.relay_fleet as rf  # noqa: E402

ROOT, N1, N2 = "cHROOT", "cHN1", "cHN2"
GOAL = "root goal: collect everything and report"
PROPOSAL = "SUBTASKS_READY\n1. first part of the work\n2. second part of the work"


def _seam():
    spec = importlib.util.spec_from_file_location(
        "seam_helpers_hier", os.path.join(REPO, "relay",
                                          "test_the_recovered_family_reaches_a_merge.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _hdr(cid, n, goal, parent=None, idx=None, depth=None, run_id="rH"):
    row = {"kind": "campaign", "campaign_id": cid, "goal": goal, "n": n, "cwd": "C:/w",
           "checks": [], "partial": "", "run_id": run_id, "ts": 1.0}
    if parent:
        row.update({"parent_campaign_id": parent, "parent_subtask_index": idx,
                    "root_id": ROOT, "depth": depth})
    return row


def _kid(cid, i, n, depth=1):
    return {"text": "%s part %d/%d" % (cid, i, n), "campaign_id": cid,
            "task_id": "%s-%d" % (cid, i), "role": "subtask", "parent_task_id": cid,
            "depth": depth, "subtask_index": i, "subtask_of": n, "cwd": "C:/w"}


def _install(monkeypatch, behave):
    """Fake workers: behave[task_id] in {"DONE", "FANOUT", "STUCK"}; default DONE."""
    h = _seam()
    h._browserless(monkeypatch)
    monkeypatch.setattr(fanout, "HIERARCHICAL_MERGE_READY", True)

    def poll(self):
        if self.status in rf.TERMINAL:
            return True
        tid = getattr(self.task_envelope, "task_id", "") or ""
        what = behave.get(tid, "DONE")
        if what == "FANOUT":
            self.display_result = self.last_response = PROPOSAL
            self.status, self.outcome = "done", "FANOUT"
        elif what == "STUCK":
            self.display_result = self.last_response = ""
            self.status, self.outcome = "stuck", "STUCK"
        else:
            self.display_result = self.last_response = "ANSWER of %s" % tid
            self._settle_done(outcome_override="DONE")
        return True

    monkeypatch.setattr(rf.RelayWorker, "poll", poll)
    return h


def _spy(monkeypatch):
    """Record every merge goal the coordinator builds: (campaign id, the records, the goal)."""
    calls = []
    real = fanout.aggregation_goal

    def spy(parent_goal, records, **kw):
        item = real(parent_goal, records, **kw)
        calls.append({"cid": kw.get("campaign_id"), "records": [dict(r) for r in records],
                      "item": item})
        return item

    monkeypatch.setattr(fanout, "aggregation_goal", spy)
    return calls


def _run(tmp_path, h, rows, goals):
    tdir = h._crashed_run(tmp_path, rows)
    return rf.run_relay_fleet(h._FakeContext(), goals, "http://agent", max_concurrent=8,
                              poll_s=0, transcript_dir=tdir, notify=lambda *a, **k: None,
                              fanout=False)


def _ledger(tmp_path):
    p = tmp_path / "campaigns.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _of(calls, cid):
    return [c for c in calls if c["cid"] == cid]


def _slot(call, idx):
    return [r for r in call["records"] if r["subtask_index"] == idx][0]


def _tree2():
    rows = [_hdr(ROOT, 2, GOAL), _hdr(N1, 2, "slot goal one", ROOT, 1, 2)]
    goals = [_kid(ROOT, 1, 2), _kid(ROOT, 2, 2), _kid(N1, 1, 2, 2), _kid(N1, 2, 2, 2)]
    return rows, goals


def _tree3():
    rows = [_hdr(ROOT, 2, GOAL), _hdr(N1, 2, "slot goal one", ROOT, 1, 2),
            _hdr(N2, 2, "slot goal deeper", N1, 1, 3)]
    goals = [_kid(ROOT, 1, 2), _kid(ROOT, 2, 2), _kid(N1, 1, 2, 2), _kid(N1, 2, 2, 2),
             _kid(N2, 1, 2, 3), _kid(N2, 2, 2, 3)]
    return rows, goals


def _no_proposal_anywhere(calls):
    for c in calls:
        assert "SUBTASKS_READY" not in c["item"]["text"]
        assert all("SUBTASKS_READY" not in (r.get("result") or "") for r in c["records"])


# ---- flat family: byte-identical to main -----------------------------------------------------

#: sha256 of the merge goal and the ledger a flat 3-slice family produced on main, taken before
#: the nested merge existed.
FLAT_GOLDEN = "30d192aa194ba00000aa2c587436ebc82f61afc222fbcb4e9115e9afbfd260cf"


def test_a_flat_family_is_byte_identical_to_main(tmp_path, monkeypatch):
    h = _install(monkeypatch, {})
    monkeypatch.setattr(fanout, "HIERARCHICAL_MERGE_READY", False)
    calls = _spy(monkeypatch)
    rows = [_hdr("cHFLAT", 3, "flat goal text")]
    goals = [_kid("cHFLAT", i, 3) for i in (1, 2, 3)]
    res = _run(tmp_path, h, rows, goals)
    texts = sorted((r.get("goal") or "") for r in res
                   if (r.get("role") or "") == "aggregator" or "分割実行の結果" in (r.get("goal") or ""))
    led = _ledger(tmp_path)
    assert len(calls) == 1
    payload = {"merge": texts, "ledger": [{k: v for k, v in r.items() if k != "ts"} for r in led]}
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                       default=str).encode("utf-8")).hexdigest()
    assert digest == FLAT_GOLDEN, digest


# ---- 2 and 3 levels, all DONE ----------------------------------------------------------------

def test_two_levels_the_slot_takes_the_nested_merge_answer(tmp_path, monkeypatch):
    h = _install(monkeypatch, {"%s-1" % ROOT: "FANOUT"})
    calls = _spy(monkeypatch)
    rows, goals = _tree2()
    _run(tmp_path, h, rows, goals)
    assert [len(_of(calls, c)) for c in (N1, ROOT)] == [1, 1], "each merge exactly once"
    assert calls.index(_of(calls, N1)[0]) < calls.index(_of(calls, ROOT)[0])
    root = _of(calls, ROOT)[0]
    assert _slot(root, 1)["outcome"] == "DONE"
    assert _slot(root, 1)["result"] == "ANSWER of %s-merge" % N1
    assert _slot(root, 2)["result"] == "ANSWER of %s-2" % ROOT
    assert "checks" not in root["item"]            # nothing missing, nothing to check
    _no_proposal_anywhere(calls)
    nested = [r for r in _ledger(tmp_path) if r.get("nested")]
    assert len(nested) == 1 and nested[0]["campaign_id"] == ROOT and nested[0]["subtask_index"] == 1


def test_three_levels_grandchild_to_child_slot_to_root_merge(tmp_path, monkeypatch):
    h = _install(monkeypatch, {"%s-1" % ROOT: "FANOUT", "%s-1" % N1: "FANOUT"})
    calls = _spy(monkeypatch)
    rows, goals = _tree3()
    _run(tmp_path, h, rows, goals)
    assert [len(_of(calls, c)) for c in (N2, N1, ROOT)] == [1, 1, 1]
    order = [c["cid"] for c in calls]
    assert order.index(N2) < order.index(N1) < order.index(ROOT)
    assert _slot(_of(calls, N1)[0], 1)["result"] == "ANSWER of %s-merge" % N2
    assert _slot(_of(calls, ROOT)[0], 1)["result"] == "ANSWER of %s-merge" % N1
    _no_proposal_anywhere(calls)


def test_a_waiting_slot_does_not_let_the_parent_merge_early(tmp_path, monkeypatch):
    """The nested family has one grandchild that never finishes: the parent must not merge."""
    h = _install(monkeypatch, {"%s-1" % ROOT: "FANOUT"})
    calls = _spy(monkeypatch)
    rows, goals = _tree2()
    _run(tmp_path, h, rows, goals[:3])              # N1 slot 2 never admitted
    assert calls == []


# ---- failure propagation ---------------------------------------------------------------------

def test_a_stuck_grandchild_marks_the_parent_slot_missing_and_the_root_names_it(tmp_path, monkeypatch):
    h = _install(monkeypatch, {"%s-1" % ROOT: "FANOUT", "%s-2" % N1: "STUCK"})
    calls = _spy(monkeypatch)
    rows, goals = _tree2()
    _run(tmp_path, h, rows, goals)
    assert [len(_of(calls, c)) for c in (N1, ROOT)] == [1, 1]
    assert fanout.missing_slices(_of(calls, N1)[0]["records"]) == [2]
    root = _of(calls, ROOT)[0]
    assert _slot(root, 1)["outcome"] == "MISSING"
    assert "2" in _slot(root, 1)["result"] and "ANSWER of %s-merge" % N1 in _slot(root, 1)["result"]
    assert fanout.missing_slices(root["records"]) == [1]
    needles = [c.get("needle") or c.get("all_of") for c in root["item"]["checks"]]
    assert ["1"] in needles and "未取得" in needles
    row = [r for r in _ledger(tmp_path) if r.get("nested")][0]
    assert row["outcome"] == "MISSING" and row["nested_missing"] == [2]
    _no_proposal_anywhere(calls)


def test_a_stuck_nested_merge_marks_the_slot_missing_not_empty_success(tmp_path, monkeypatch):
    h = _install(monkeypatch, {"%s-1" % ROOT: "FANOUT", "%s-merge" % N1: "STUCK"})
    calls = _spy(monkeypatch)
    rows, goals = _tree2()
    _run(tmp_path, h, rows, goals)
    assert len(_of(calls, ROOT)) == 1
    root = _of(calls, ROOT)[0]
    assert _slot(root, 1)["outcome"] == "MISSING" and _slot(root, 1)["result"] == ""
    assert fanout.missing_slices(root["records"]) == [1]
    assert root["item"]["checks"], "the root merge must be gated on naming the gap"
    nested = [r for r in _ledger(tmp_path) if r.get("nested")]
    assert len(nested) == 1 and nested[0]["outcome"] == "MISSING", "written once, not per sweep"
    _no_proposal_anywhere(calls)


def test_a_split_proposal_is_never_a_slots_answer():
    row = fanout.nested_result_row(ROOT, 1, N1, PROPOSAL)
    assert row["outcome"] == "MISSING" and row["result"] == ""
    rec = fanout.slot_record(1, {"outcome": "DONE", "result": PROPOSAL, "nested": N1})
    assert rec["outcome"] == "MISSING" and rec["finished"] and rec["result"] == ""
    assert fanout.slot_record(1, None, nested_cid=N1)["finished"] is False
    gone = fanout.slot_record(1, None)
    assert gone["finished"] and gone["outcome"] == "MISSING"
    ok = fanout.nested_result_row(ROOT, 1, N1, "the real answer")
    assert ok["outcome"] == "DONE" and ok["result"] == "the real answer"


# ---- exactly once and resume -----------------------------------------------------------------

def _finished_nested(cid, merged_key="K1"):
    return [{"kind": "merged", "campaign_id": cid, "agg_key": merged_key},
            {"kind": "merge_done", "campaign_id": cid}]


def test_resume_after_a_nested_merge_issues_neither_it_nor_the_root_twice(tmp_path, monkeypatch):
    h = _install(monkeypatch, {})
    calls = _spy(monkeypatch)
    rows = [_hdr(ROOT, 2, GOAL), _hdr(N1, 2, "slot goal one", ROOT, 1, 2)]
    rows += _finished_nested(N1)
    rows += [fanout.nested_result_row(ROOT, 1, N1, "NESTED ANSWER")]
    _run(tmp_path, h, rows, [_kid(ROOT, 2, 2)])      # only the root's other slice re-queued
    assert [c["cid"] for c in calls] == [ROOT], "no second nested merge, root merge once"
    assert _slot(calls[0], 1)["result"] == "NESTED ANSWER"
    led = _ledger(tmp_path)
    assert len([r for r in led if r.get("kind") == "merged" and r["campaign_id"] == N1]) == 1
    assert len([r for r in led if r.get("kind") == "merged" and r["campaign_id"] == ROOT]) == 1


def test_resume_mid_tree_requeues_only_unfinished_grandchildren(tmp_path, monkeypatch):
    h = _install(monkeypatch, {})
    calls = _spy(monkeypatch)
    rows, _ = _tree3()
    rows += _finished_nested(N2)
    rows += [fanout.nested_result_row(N1, 1, N2, "NESTED ANSWER N2")]
    _run(tmp_path, h, rows, [_kid(ROOT, 2, 2), _kid(N1, 2, 2, 2)])
    assert [c["cid"] for c in calls] == [N1, ROOT]
    assert _slot(calls[0], 1)["result"] == "NESTED ANSWER N2"
    assert _slot(calls[1], 1)["result"] == "ANSWER of %s-merge" % N1
    _no_proposal_anywhere(calls)


def _ledger_with_children(tmp_path, nested=True):
    rows = [_hdr(ROOT, 2, GOAL)]
    kids = [_kid(ROOT, 1, 2), _kid(ROOT, 2, 2)]
    for k in kids:
        rows.append({"campaign_id": ROOT, "task_id": k["task_id"],
                     "subtask_index": k["subtask_index"], "text": k["text"], "goal": k})
    if nested:
        rows.append(_hdr(N1, 2, "slot goal one", ROOT, 1, 2, run_id="rOTHER"))
    with io.open(str(tmp_path / "campaigns.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return kids


def test_resume_does_not_requeue_a_child_that_split(tmp_path):
    kids = _ledger_with_children(tmp_path)
    done = {fr.goal_resume_key(kids[0]): "FANOUT"}
    goals, _ = fr.resume_children_goals(str(tmp_path), done, log=lambda m: None,
                                        scope={ROOT, N1})
    assert [g["subtask_index"] for g in goals] == [2]


def test_a_child_that_split_without_a_nested_family_is_still_requeued(tmp_path):
    kids = _ledger_with_children(tmp_path, nested=False)
    done = {fr.goal_resume_key(kids[0]): "FANOUT"}
    goals, _ = fr.resume_children_goals(str(tmp_path), done, log=lambda m: None, scope={ROOT})
    assert sorted(g["subtask_index"] for g in goals) == [1, 2]


def test_the_scope_follows_the_parent_campaign_chain(tmp_path):
    _ledger_with_children(tmp_path)
    fams = fr.read_campaigns(str(tmp_path))
    got = fr.campaigns_of_run(fams, run_ids={"rH"})
    assert got == {ROOT, N1}, "the nested family was stamped by another run but hangs below"
    assert fr.campaigns_of_run(fams, run_ids={"rZ"}) == set()


def test_a_finished_nested_merge_without_a_slot_row_is_sealed_missing_once(tmp_path):
    _ledger_with_children(tmp_path)
    with io.open(str(tmp_path / "campaigns.jsonl"), "a", encoding="utf-8", newline="\n") as fh:
        for r in _finished_nested(N1):
            fh.write(json.dumps(r) + "\n")
    for _ in range(2):
        fr.resume_children_goals(str(tmp_path), {}, log=lambda m: None, scope={ROOT, N1})
    rows = [r for r in _ledger(tmp_path) if r.get("nested")]
    assert len(rows) == 1 and rows[0]["outcome"] == "MISSING"
    assert rows[0]["campaign_id"] == ROOT and rows[0]["subtask_index"] == 1


# ---- budget refusal inside a nested level ----------------------------------------------------

def test_a_budget_refusal_inside_a_nested_level_still_runs_directly(tmp_path, monkeypatch):
    import time
    lim = {"total": 8, "active": 3, "turns": 400, "wall_min": 120}
    steps = ["a" * 10, "b" * 10, "c" * 10]
    camps = [{"kind": "campaign", "campaign_id": "R", "root_id": "R", "n": 1, "ts": time.time()}]
    rows7 = [{"root_id": "R", "status": "done", "turn": 1} for _ in range(7)]
    use, why = fb.apply_budget(steps, "R", rows7, camps, lim, min_children=2, self_active=1)
    assert use == [] and why, "refused: the worker runs its slice itself"
    # ...so its slot ends DONE (never FANOUT) and the root merge reads the real answer
    h = _install(monkeypatch, {})
    calls = _spy(monkeypatch)
    _run(tmp_path, h, [_hdr(ROOT, 2, GOAL)], [_kid(ROOT, 1, 2), _kid(ROOT, 2, 2)])
    assert len(calls) == 1 and _slot(calls[0], 1)["result"] == "ANSWER of %s-1" % ROOT


# ---- the switch stays off in production ------------------------------------------------------

def test_production_code_never_turns_the_hierarchical_switch_on():
    assert fanout.HIERARCHICAL_MERGE_READY is False
    out = []
    for dp, dns, fns in os.walk(REPO):
        dns[:] = [d for d in dns if not d.startswith(".") and d not in ("node_modules", "venv")]
        out += [os.path.relpath(os.path.join(dp, f), REPO) for f in fns if f.endswith(".py")]
    pat = re.compile(r"HIERARCHICAL_MERGE_READY\s*(?:=|,)\s*True|"
                     r"setattr\([^)]*HIERARCHICAL_MERGE_READY[^)]*True")
    bad = []
    for rel in out:
        parts = rel.replace("\\", "/").split("/")
        if parts[-1].startswith("test_") or "tests" in parts[:-1]:
            continue
        try:
            text = open(os.path.join(REPO, rel), encoding="utf-8").read()
        except OSError:
            continue
        if pat.search(text):
            bad.append(rel)
    assert bad == [], "production code must not set HIERARCHICAL_MERGE_READY True: %s" % bad
