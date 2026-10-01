# -*- coding: utf-8 -*-
"""Adversarial synthetic families for the hierarchical merge: crashes, retries, budgets.

tests/test_hierarchical_merge.py holds the happy paths and the first failure modes. This file is
the gap audit: the scenarios it did not cover, each driven through run_relay_fleet (or the resume
functions a fresh process calls) with fake workers and hand-written ledgers, the switch
HIERARCHICAL_MERGE_READY monkeypatched True.

  1. a crash between a grandchild finishing and its child_result being written (depth 3)
  2. a crash after a nested merge finished but before its parent slot row was written: the answer
     is recovered from the merge worker's traces, MISSING only when nothing is recoverable
  3. a retry of a depth-1 child (new task id) that already has a nested family
  4. a grandchild whose split the budget refuses: the parent slot gets a direct answer
  5. two siblings splitting in the same sweep: the root's budget is not granted twice
  6. a nested merge STUCK, then retried: the slot goes MISSING -> real answer exactly once and the
     root merge is not re-issued silently
  7. resume of a family whose root merge is already done: nothing is queued
"""
from __future__ import annotations

import io
import json
import os
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import fanout, fanout_budget as fb  # noqa: E402
from relay import fleet_resume as fr  # noqa: E402
import relay.relay_fleet as rf  # noqa: E402
from tests import test_hierarchical_merge as thm  # noqa: E402

ROOT, N1, N2 = thm.ROOT, thm.N1, thm.N2
STEPS = ["first half of the slice, with enough words to be a step",
         "second half of the slice, with enough words to be a step"]


# ---- harness -------------------------------------------------------------------------------

def _hdr(cid, n, goal, parent=None, idx=None, depth=None, run_id="rH"):
    row = thm._hdr(cid, n, goal, parent, idx, depth, run_id)
    row["ts"] = time.time()              # a real start time: the budget reads the wall clock
    return row


def _write_ledger(tmp_path, rows):
    with io.open(str(tmp_path / "campaigns.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _child_row(kid):
    return {"campaign_id": kid["campaign_id"], "task_id": kid["task_id"],
            "subtask_index": kid["subtask_index"], "text": kid["text"], "goal": kid}


def _done_map(tmp_path, entries):
    """last_run_done.json: {resume key: outcome}."""
    data = {fr.goal_resume_key(g): oc for g, oc in entries}
    (tmp_path / "last_run_done.json").write_text(json.dumps(data), encoding="utf-8")
    return data


def _outcome_file(tmp_path, jid, text):
    d = tmp_path / "tasks" / "done"
    d.mkdir(parents=True, exist_ok=True)
    (d / (jid + ".outcome.json")).write_text(
        json.dumps({"status": "done", "result": {"answer": text}}), encoding="utf-8")


def _transcript(tmp_path, name, first_user, last_assistant):
    d = tmp_path / "transcripts"
    d.mkdir(parents=True, exist_ok=True)
    rows = [{"role": "user", "text": first_user}, {"role": "assistant", "text": last_assistant}]
    (d / name).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                          encoding="utf-8")


def _split_install(monkeypatch, behave, limits=None):
    """Like thm._install, plus behave[task_id] == "SPLIT": the worker splits the way the real
    worker does (budget grant -> child_goals with its place in the tree -> the coordinator's
    spawn), ending FANOUT, or runs the slice directly when the budget refuses."""
    h = thm._install(monkeypatch, behave)
    from relay import fleet_runner as FR
    real_int = FR._settings_int
    monkeypatch.setattr(FR, "_settings_int", lambda key, default: (
        3 if key == fanout.DEPTH_SETTING_KEY else real_int(key, default)))
    if limits is not None:
        monkeypatch.setattr(fb, "limits_from_settings", lambda: dict(limits))
    plain_poll = rf.RelayWorker.poll

    def poll(self):
        if self.status in rf.TERMINAL:
            return True
        env = self.task_envelope
        tid = getattr(env, "task_id", "") or ""
        if behave.get(tid) != "SPLIT":
            return plain_poll(self)
        tree = {}
        if int(getattr(env, "depth", 0) or 0) > 0:
            tree = {"depth": int(env.depth), "parent_campaign_id": env.campaign_id or "",
                    "parent_subtask_index": self.subtask_index,
                    "root_id": (env.metadata or {}).get("root_id") or env.campaign_id or ""}
        steps, why = self._spawn_fn.grant(
            self.goal, list(STEPS), tid, **({"root_id": tree["root_id"]} if tree else {}))
        kids = fanout.child_goals(self.goal, steps, parent_task_id=tid, cwd="C:/w", **tree) \
            if steps else []
        if kids:
            self._spawn_fn(self.goal, kids, parent_checks=None, parent_partial="")
            self.display_result = self.last_response = thm.PROPOSAL
            self.status, self.outcome = "done", "FANOUT"
        else:
            self.display_result = self.last_response = "DIRECT of %s (%s)" % (tid, why)
            self._settle_done(outcome_override="DONE")
        return True

    monkeypatch.setattr(rf.RelayWorker, "poll", poll)
    return h


def _fleet(tmp_path, h, goals, add_box=None):
    tdir = str(tmp_path / "transcripts")
    os.makedirs(tdir, exist_ok=True)
    return rf.run_relay_fleet(h._FakeContext(), goals, "http://agent", max_concurrent=8,
                              poll_s=0, transcript_dir=tdir, notify=lambda *a, **k: None,
                              fanout=False, **({"add_box": add_box} if add_box is not None else {}))


def _nested_rows(tmp_path, pc=ROOT, idx=1):
    return [r for r in thm._ledger(tmp_path)
            if r.get("kind") == "child_result" and r.get("nested")
            and r["campaign_id"] == pc and r["subtask_index"] == idx]


def _tree3_rows():
    return [_hdr(ROOT, 2, thm.GOAL), _hdr(N1, 2, "slot goal one", ROOT, 1, 2),
            _hdr(N2, 2, "slot goal deeper", N1, 1, 3)]


def _all_kids3():
    return [thm._kid(ROOT, 1, 2), thm._kid(ROOT, 2, 2), thm._kid(N1, 1, 2, 2),
            thm._kid(N1, 2, 2, 2), thm._kid(N2, 1, 2, 3), thm._kid(N2, 2, 2, 3)]


# ---- 1. depth 3: a grandchild finished, its child_result line never reached the disk ---------

def test_1_a_crash_between_a_grandchild_finishing_and_its_result_line_at_depth_3(tmp_path, monkeypatch):
    h = thm._install(monkeypatch, {})
    calls = thm._spy(monkeypatch)
    kids = _all_kids3()
    _write_ledger(tmp_path, _tree3_rows() + [_child_row(k) for k in kids])
    # N2 slice 1 finished DONE (the done-map says so) but no `child_result` line was written;
    # its answer is still in the task's outcome file. The two splitting slices ended FANOUT.
    _done_map(tmp_path, [(kids[0], "FANOUT"), (kids[2], "FANOUT"), (kids[4], "DONE")])
    _outcome_file(tmp_path, "%s-1" % N2, "RECOVERED GRANDCHILD ANSWER")
    goals, _ = fr.resume_children_goals(str(tmp_path), log=lambda m: None,
                                        scope={ROOT, N1, N2})
    assert sorted(g["task_id"] for g in goals) == ["%s-2" % N1, "%s-2" % N2, "%s-2" % ROOT], \
        "only the unfinished slices are re-queued; the recovered one and the splitters are not"
    recovered = [r for r in thm._ledger(tmp_path) if r.get("recovered")]
    assert len(recovered) == 1 and recovered[0]["campaign_id"] == N2
    assert recovered[0]["subtask_index"] == 1 and recovered[0]["source"] == "outcome_json"
    _fleet(tmp_path, h, goals)
    assert [c["cid"] for c in calls] == [N2, N1, ROOT], "each merge exactly once, bottom-up"
    assert _slot(calls, N2, 1)["result"] == "RECOVERED GRANDCHILD ANSWER"
    assert _slot(calls, N1, 1)["result"] == "ANSWER of %s-merge" % N2
    assert _slot(calls, ROOT, 1)["result"] == "ANSWER of %s-merge" % N1
    thm._no_proposal_anywhere(calls)


def _slot(calls, cid, idx):
    return thm._slot(thm._of(calls, cid)[0], idx)


def test_1b_an_unrecoverable_grandchild_is_rerun_once_not_lost(tmp_path, monkeypatch):
    h = thm._install(monkeypatch, {})
    calls = thm._spy(monkeypatch)
    kids = _all_kids3()
    _write_ledger(tmp_path, _tree3_rows() + [_child_row(k) for k in kids])
    _done_map(tmp_path, [(kids[0], "FANOUT"), (kids[2], "FANOUT"), (kids[4], "DONE")])
    goals, _ = fr.resume_children_goals(str(tmp_path), log=lambda m: None,
                                        scope={ROOT, N1, N2})
    assert "%s-1" % N2 in [g["task_id"] for g in goals], "no answer anywhere: re-queued"
    again, _ = fr.resume_children_goals(str(tmp_path), log=lambda m: None,
                                        scope={ROOT, N1, N2})
    assert "%s-1" % N2 not in [g["task_id"] for g in again], "...but only once"
    _fleet(tmp_path, h, goals)
    assert [c["cid"] for c in calls] == [N2, N1, ROOT]


# ---- 2. nested merge finished, parent slot row not written -----------------------------------

def _nested_finished_ledger(tmp_path):
    kids = [thm._kid(ROOT, 1, 2), thm._kid(ROOT, 2, 2)]
    rows = [_hdr(ROOT, 2, thm.GOAL), _hdr(N1, 2, "slot goal one", ROOT, 1, 2)]
    rows += [_child_row(k) for k in kids]
    rows += thm._finished_nested(N1)
    _write_ledger(tmp_path, rows)
    return kids


def _seal(tmp_path):
    return fr.resume_children_goals(str(tmp_path), {}, log=lambda m: None, scope={ROOT, N1})


@pytest.mark.parametrize("where", ["outcome_json", "transcript", "history"])
def test_2_the_nested_merge_answer_is_recovered_before_the_slot_is_sealed_missing(
        tmp_path, monkeypatch, where):
    _nested_finished_ledger(tmp_path)
    text = "NESTED MERGE ANSWER via %s" % where
    if where == "outcome_json":
        _outcome_file(tmp_path, "%s-merge" % N1, text)
    elif where == "transcript":
        _transcript(tmp_path, "w0.jsonl", "slot goal one\n\n" + fanout.aggregation_prompt(
            "slot goal one", [{"outcome": "DONE", "subtask_index": 1, "result": "x"}]), text)
    else:
        (tmp_path / "history.json").write_text(json.dumps(
            [{"jid": "%s-merge" % N1, "outcome": "DONE", "goal": "g", "display_result": text}]),
            encoding="utf-8")
    for _ in range(2):
        _seal(tmp_path)
    rows = _nested_rows(tmp_path)
    assert len(rows) == 1, "written once"
    assert rows[0]["outcome"] == "DONE" and rows[0]["result"] == text
    assert rows[0]["recovered"] is True and rows[0]["source"] == where
    # and the root merge, once its other slice is in, reads that answer
    h = thm._install(monkeypatch, {})
    calls = thm._spy(monkeypatch)
    _fleet(tmp_path, h, [thm._kid(ROOT, 2, 2)])
    assert [c["cid"] for c in calls] == [ROOT]
    assert _slot(calls, ROOT, 1)["result"] == text and _slot(calls, ROOT, 1)["outcome"] == "DONE"


def test_2_the_splitting_workers_transcript_is_never_taken_for_the_merge(tmp_path):
    """The splitting child was handed the SAME goal text; its last turn is a split proposal."""
    _nested_finished_ledger(tmp_path)
    _transcript(tmp_path, "w1.jsonl", "slot goal one\n\n【この会話が担当する範囲】", thm.PROPOSAL)
    _seal(tmp_path)
    rows = _nested_rows(tmp_path)
    assert len(rows) == 1 and rows[0]["outcome"] == "MISSING" and rows[0]["result"] == ""
    assert rows[0]["sealed"] == "nested_merge_result_not_recorded"


def test_2_a_recovered_split_proposal_is_refused_and_nothing_recoverable_seals_missing(tmp_path):
    _nested_finished_ledger(tmp_path)
    _outcome_file(tmp_path, "%s-merge" % N1, thm.PROPOSAL)
    _seal(tmp_path)
    rows = _nested_rows(tmp_path)
    assert len(rows) == 1 and rows[0]["outcome"] == "MISSING" and rows[0]["result"] == ""


def test_2_a_merge_that_went_without_slices_recovers_its_text_but_stays_missing(tmp_path):
    kids = _nested_finished_ledger(tmp_path)
    with io.open(str(tmp_path / "campaigns.jsonl"), "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"kind": "merged", "campaign_id": N1, "agg_key": "K2",
                             "missing": [2]}) + "\n")
    _outcome_file(tmp_path, "%s-merge" % N1, "partial account, slice 2 未取得")
    _seal(tmp_path)
    row = _nested_rows(tmp_path)[0]
    assert row["outcome"] == "MISSING" and row["nested_missing"] == [2]
    assert "partial account" in row["result"]
    assert kids


# ---- 3. a retried depth-1 child with a nested family -----------------------------------------

def test_3_a_retry_of_a_split_child_does_not_make_a_second_nested_family(tmp_path, monkeypatch):
    h = _split_install(monkeypatch, {"%s-1" % ROOT: "SPLIT", "%s-1r" % ROOT: "SPLIT"})
    calls = thm._spy(monkeypatch)
    _write_ledger(tmp_path, [_hdr(ROOT, 2, thm.GOAL)])
    first = thm._kid(ROOT, 1, 2)
    retry = dict(thm._kid(ROOT, 1, 2), task_id="%s-1r" % ROOT)       # new task id, same slot
    _fleet(tmp_path, h, [first, retry, thm._kid(ROOT, 2, 2)])
    heads = [r for r in thm._ledger(tmp_path) if r.get("kind") == "campaign"
             and r.get("parent_campaign_id") == ROOT and r.get("parent_subtask_index") == 1]
    assert len(heads) == 1, "one nested family per parent slot"
    nested_cid = heads[0]["campaign_id"]
    assert [c["cid"] for c in calls].count(nested_cid) == 1
    assert [c["cid"] for c in calls].count(ROOT) == 1, "the root merge happens once"
    root = thm._of(calls, ROOT)[0]
    assert thm._slot(root, 1)["outcome"] == "DONE"
    assert thm._slot(root, 1)["result"].startswith("ANSWER of %s-merge" % nested_cid)
    thm._no_proposal_anywhere(calls)
    assert len([r for r in thm._ledger(tmp_path) if r.get("kind") == "merged"
                and r["campaign_id"] == nested_cid]) == 1, "the first family is not orphaned"


def test_3_a_retry_in_a_later_run_finds_the_family_on_the_ledger(tmp_path, monkeypatch):
    h = _split_install(monkeypatch, {"%s-1r" % ROOT: "SPLIT"})
    _write_ledger(tmp_path, [_hdr(ROOT, 2, thm.GOAL),
                             _hdr("cHEARLIER", 2, "slot goal one", ROOT, 1, 2)])
    retry = dict(thm._kid(ROOT, 1, 2), task_id="%s-1r" % ROOT)
    _fleet(tmp_path, h, [retry])
    heads = [r for r in thm._ledger(tmp_path) if r.get("kind") == "campaign"
             and r.get("parent_campaign_id") == ROOT and r.get("parent_subtask_index") == 1]
    assert [r["campaign_id"] for r in heads] == ["cHEARLIER"], "no second nested header"


# ---- 4. budget refusal mid-tree --------------------------------------------------------------

def test_4_a_grandchild_refused_by_the_budget_gives_its_slot_a_direct_answer(tmp_path, monkeypatch):
    # room for the root's own two slices and one nested family of two, nothing deeper
    lim = {"total": 5, "active": 50, "turns": 400, "wall_min": 120}
    class _SplitFirstSlices:
        """Slice 1 of every family splits (nested ids are hashes, so match by shape)."""
        def get(self, tid, default=None):
            return "SPLIT" if tid.endswith("-1") else default

    h = _split_install(monkeypatch, _SplitFirstSlices(), lim)
    calls = thm._spy(monkeypatch)
    _write_ledger(tmp_path, [_hdr(ROOT, 2, thm.GOAL)])
    _fleet(tmp_path, h, [thm._kid(ROOT, 1, 2), thm._kid(ROOT, 2, 2)])
    heads = [r for r in thm._ledger(tmp_path) if r.get("kind") == "campaign"
             and r.get("parent_campaign_id")]
    assert len(heads) == 1, "the grandchild's split was refused, so no third level exists"
    n1 = heads[0]["campaign_id"]
    inner = thm._of(calls, n1)[0]
    slot = thm._slot(inner, 1)
    assert slot["outcome"] == "DONE" and slot["result"].startswith("DIRECT of %s-1" % n1)
    assert "budget" not in slot["result"] or "limit" in slot["result"]
    assert [c["cid"] for c in calls] == [n1, ROOT]
    thm._no_proposal_anywhere(calls)
    assert thm._slot(thm._of(calls, ROOT)[0], 1)["result"].startswith("ANSWER of %s-merge" % n1)


# ---- 5. two siblings splitting in the same sweep ---------------------------------------------

def test_5_two_siblings_splitting_together_are_not_both_granted_the_same_room(tmp_path, monkeypatch):
    lim = {"total": 6, "active": 50, "turns": 400, "wall_min": 120}
    h = _split_install(monkeypatch, {"%s-1" % ROOT: "SPLIT", "%s-2" % ROOT: "SPLIT"}, lim)
    calls = thm._spy(monkeypatch)
    _write_ledger(tmp_path, [_hdr(ROOT, 2, thm.GOAL)])
    _fleet(tmp_path, h, [thm._kid(ROOT, 1, 2), thm._kid(ROOT, 2, 2)])
    heads = [r for r in thm._ledger(tmp_path) if r.get("kind") == "campaign"
             and r.get("parent_campaign_id") == ROOT]
    assert len(heads) == 1, "second sibling saw the first's header: refused, ran directly"
    rows = [r for r in thm._ledger(tmp_path) if r.get("kind") == "campaign"]
    assert sum(int(r["n"]) for r in rows) + fb.MERGE_RESERVE <= lim["total"]
    root = thm._of(calls, ROOT)[0]
    assert sorted(r["outcome"] for r in root["records"]) == ["DONE", "DONE"]
    assert any(r["result"].startswith("DIRECT of") for r in root["records"])
    # the same through the pure function the coordinator uses, on the root's real usage
    usage = fb.usage_from_status([], [dict(r) for r in rows])
    granted, _ = fb.grant(ROOT, 2, usage, lim)
    assert granted < fanout.MIN_CHILDREN, "no room for a second split after the first one"


def test_5_with_room_for_both_each_sibling_splits_once(tmp_path, monkeypatch):
    lim = {"total": 10, "active": 50, "turns": 400, "wall_min": 120}
    h = _split_install(monkeypatch, {"%s-1" % ROOT: "SPLIT", "%s-2" % ROOT: "SPLIT"}, lim)
    calls = thm._spy(monkeypatch)
    _write_ledger(tmp_path, [_hdr(ROOT, 2, thm.GOAL)])
    _fleet(tmp_path, h, [thm._kid(ROOT, 1, 2), thm._kid(ROOT, 2, 2)])
    heads = [r for r in thm._ledger(tmp_path) if r.get("kind") == "campaign"
             and r.get("parent_campaign_id") == ROOT]
    assert sorted(r["parent_subtask_index"] for r in heads) == [1, 2]
    assert [c["cid"] for c in calls].count(ROOT) == 1 and len(calls) == 3


# ---- 6. nested merge STUCK, then retried -----------------------------------------------------

def _count(tmp_path, kind, cid):
    return len([r for r in thm._ledger(tmp_path) if r.get("kind") == kind
                and r.get("campaign_id") == cid])


def test_6_a_retried_nested_merge_replaces_missing_with_the_answer_once(tmp_path, monkeypatch):
    h = thm._install(monkeypatch, {"%s-1" % ROOT: "FANOUT", "%s-merge" % N1: "STUCK"})
    calls = thm._spy(monkeypatch)
    rows, goals = thm._tree2()
    _write_ledger(tmp_path, [dict(r, ts=time.time()) for r in rows])
    _fleet(tmp_path, h, goals)
    assert [c["cid"] for c in calls] == [N1, ROOT]
    assert thm._slot(calls[1], 1)["outcome"] == "MISSING"
    first = _nested_rows(tmp_path)
    assert len(first) == 1 and first[0]["outcome"] == "MISSING"
    assert _count(tmp_path, "merged", ROOT) == 1

    # a fresh process; the nested merge is retried and now finishes DONE
    h2 = thm._install(monkeypatch, {})
    n_before = len(calls)
    _fleet(tmp_path, h2, [])
    assert [c["cid"] for c in calls[n_before:]] == [N1], "the nested merge is re-issued once"
    rows_after = _nested_rows(tmp_path)
    assert [r["outcome"] for r in rows_after] == ["MISSING", "DONE"], "MISSING -> answer, once"
    assert rows_after[-1]["result"] == "ANSWER of %s-merge" % N1
    # DECISION: the root merge that was already issued with the gap is NOT re-issued here
    assert [c["cid"] for c in calls].count(ROOT) == 1
    assert _count(tmp_path, "merged", ROOT) == 1 and _count(tmp_path, "merge_requeued", ROOT) == 0
    assert _count(tmp_path, "merge_requeued", N1) == 1, "the nested merge used its one re-issue"

    # and a third process changes nothing further
    n_mid = len(calls)
    _fleet(tmp_path, h2, [])
    assert len(calls) == n_mid and len(_nested_rows(tmp_path)) == 2


def test_6_a_nested_merge_retried_in_the_same_process_replaces_missing_once(tmp_path, monkeypatch):
    """The parent family is still in memory (it merged with the gap moments ago): the old
    'a nested row exists, so write nothing' rule left the slot MISSING for good."""
    add_box = []
    retry = fanout.aggregation_goal("slot goal one", [{"outcome": "DONE", "subtask_index": 1,
                                                       "result": "x"}], campaign_id=N1, depth=2)

    class _StuckThenDone:
        def __init__(self):
            self.seen = 0

        def get(self, tid, default=None):
            if tid == "%s-merge" % N1:
                self.seen += 1
                if self.seen == 1:
                    add_box.append(dict(retry))          # the operator retries the merge
                    return "STUCK"
                return "DONE"
            return "FANOUT" if tid == "%s-1" % ROOT else default

    h = thm._install(monkeypatch, _StuckThenDone())
    calls = thm._spy(monkeypatch)
    rows, goals = thm._tree2()
    _write_ledger(tmp_path, [dict(r, ts=time.time()) for r in rows])
    _fleet(tmp_path, h, goals, add_box=add_box)
    out = _nested_rows(tmp_path)
    assert [r["outcome"] for r in out] == ["MISSING", "DONE"], "MISSING -> answer, exactly once"
    assert out[-1]["result"] == "ANSWER of %s-merge" % N1
    assert [c["cid"] for c in calls].count(ROOT) == 1, "the root merge is not re-issued silently"
    assert _count(tmp_path, "merged", ROOT) == 1 and _count(tmp_path, "merge_requeued", ROOT) == 0


def test_6_a_nested_merge_that_is_retried_after_its_slot_is_answered_writes_nothing(tmp_path, monkeypatch):
    """A DONE slot row is never superseded (nor duplicated) by a second finishing merge."""
    h = thm._install(monkeypatch, {"%s-1" % ROOT: "FANOUT"})
    calls = thm._spy(monkeypatch)
    rows, goals = thm._tree2()
    _write_ledger(tmp_path, [dict(r, ts=time.time()) for r in rows])
    _fleet(tmp_path, h, goals)
    assert len(_nested_rows(tmp_path)) == 1
    retry = fanout.aggregation_goal("slot goal one", [{"outcome": "DONE", "subtask_index": 1,
                                                       "result": "x"}], campaign_id=N1, depth=2)
    _fleet(tmp_path, h, [retry])
    assert len(_nested_rows(tmp_path)) == 1, "the slot already has its answer"
    assert [c["cid"] for c in calls].count(ROOT) == 1


# ---- 7. resume over a family whose root merge is already done --------------------------------

def test_7_resume_of_a_fully_merged_family_queues_nothing(tmp_path, monkeypatch):
    h = thm._install(monkeypatch, {})
    calls = thm._spy(monkeypatch)
    kids = _all_kids3()
    rows = _tree3_rows() + [_child_row(k) for k in kids]
    for cid, parent in ((N2, N1), (N1, ROOT)):
        rows += thm._finished_nested(cid)
        rows.append(fanout.nested_result_row(parent, 1, cid, "ANSWER %s" % cid))
    rows += thm._finished_nested(ROOT)
    _write_ledger(tmp_path, rows)
    # a done-map that calls every slice DONE although only some have result lines
    _done_map(tmp_path, [(k, "DONE") for k in kids])
    before = (tmp_path / "campaigns.jsonl").read_bytes()
    goals, _ = fr.resume_children_goals(str(tmp_path), log=lambda m: None,
                                        scope={ROOT, N1, N2})
    assert goals == []
    assert (tmp_path / "campaigns.jsonl").read_bytes() == before, "nothing is written either"
    _fleet(tmp_path, h, [])
    assert calls == []
    assert (tmp_path / "campaigns.jsonl").read_bytes() == before
