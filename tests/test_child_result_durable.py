# -*- coding: utf-8 -*-
"""A finished child's answer must be on the campaign ledger, or recoverable, or re-run once.

THE DEFECT (found by a live resume test): the `child_result` ledger line was written on a LATER
sweep, not when the child's outcome was set. A coordinator killed in that window left the
done-map saying DONE with no answer on disk; resume skipped the child as finished, the merge
queue saw n-1 records and waited forever with no error.

Three layers are held here:
  1. durability   -- the worker writes the line from _settle_done itself (the sweep is a
                     safety net and never duplicates a line)
  2. resume       -- a DONE child with no line gets its answer recovered (outcome file,
                     transcript, history.json) or is re-queued ONCE
  3. stall        -- the merge queue notices a family short of records whose missing children
                     are all DONE, records the mechanism and runs the same recovery

Hermetic: tmp_path fleet directories; the sweep tests use the browserless harness of
relay/test_the_recovered_family_reaches_a_merge.py.
"""
from __future__ import annotations

import gzip
import importlib.util
import io
import json
import os
import sys
import types

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import fanout, fleet_resume as fr  # noqa: E402
from relay import mechanism_telemetry as MT  # noqa: E402
import relay.relay_fleet as rf  # noqa: E402

CID = "cDUR"
PARENT = "split me into two"


def _child(i, n=2):
    text = "child %d of %s" % (i, CID)
    return {"campaign_id": CID, "task_id": "%s-%d" % (CID, i), "subtask_index": i,
            "text": text,
            "goal": {"text": text, "cwd": "C:/w", "campaign_id": CID, "role": "subtask",
                     "subtask_index": i, "subtask_of": n, "depth": 1,
                     "task_id": "%s-%d" % (CID, i)}}


def _header(n=2):
    return {"kind": "campaign", "campaign_id": CID, "goal": PARENT, "n": n, "cwd": "C:/w",
            "checks": [], "partial": ""}


def _write_rows(state, rows):
    with io.open(str(state / "campaigns.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _ledger(state):
    p = state / "campaigns.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _done(*idx):
    return {fr.goal_resume_key(_child(i)["goal"]): "DONE" for i in idx}


@pytest.fixture
def mech(tmp_path, monkeypatch):
    path = tmp_path / "mech.jsonl"
    monkeypatch.setattr(MT, "LOG", str(path), raising=False)
    return path


def _mech_rows(path):
    if not os.path.isfile(str(path)):
        return []
    return [json.loads(l) for l in open(str(path), encoding="utf-8") if l.strip()]


def _resume(state, done):
    return fr.resume_children_goals(str(state), done, log=lambda m: None, scope={CID})


def _result_line(i, text="answer"):
    return {"kind": "child_result", "campaign_id": CID, "subtask_index": i,
            "outcome": "DONE", "result": text}


# ---- 2. resume: unchanged cases ------------------------------------------------------------

def test_done_with_a_result_line_is_unchanged(tmp_path, mech):
    _write_rows(tmp_path, [_header(), _child(1), _child(2), _result_line(1)])
    goals, _ = _resume(tmp_path, _done(1))
    assert [g["subtask_index"] for g in goals] == [2]
    assert [r.get("kind") for r in _ledger(tmp_path)].count("child_result") == 1
    assert _mech_rows(mech) == []


def test_old_ledgers_without_the_new_marker_still_load(tmp_path):
    _write_rows(tmp_path, [_header(), _child(1), _result_line(1),
                           {"kind": "merged", "campaign_id": CID}])
    fam = fanout.campaigns_from_ledger(open(str(tmp_path / "campaigns.jsonl"), encoding="utf-8"))[CID]
    assert len(fam["children"]) == 1 and "child_requeued" not in fam


def test_the_requeue_marker_is_not_a_child(tmp_path):
    lines = [json.dumps(r) for r in [
        {"kind": "child_requeued", "campaign_id": CID, "subtask_index": 1}, _header(),
        _child(1)]]
    fam = fanout.campaigns_from_ledger(lines)[CID]
    assert len(fam["children"]) == 1 and len(fam["child_requeued"]) == 1


# ---- 2. resume: recovery order -------------------------------------------------------------

def _outcome_file(state, jid, answer):
    d = state / "tasks" / "done"
    d.mkdir(parents=True, exist_ok=True)
    (d / (jid + ".outcome.json")).write_text(
        json.dumps({"id": jid, "status": "done", "result": {"answer": answer}}), encoding="utf-8")


def _transcript(state, name, goal_text, answer, gz=False):
    d = state / "transcripts"
    d.mkdir(exist_ok=True)
    body = "\n".join(json.dumps(r) for r in [
        {"turn": 1, "role": "user", "text": "contract...\n" + goal_text},
        {"turn": 1, "role": "assistant", "text": "first draft"},
        {"turn": 2, "role": "assistant", "text": answer}]) + "\n"
    if gz:
        with gzip.open(str(d / (name + ".jsonl.gz")), "wt", encoding="utf-8") as fh:
            fh.write(body)
    else:
        (d / (name + ".jsonl")).write_text(body, encoding="utf-8")


def _history(state, goal_text, answer):
    (state / "history.json").write_text(json.dumps([
        {"goal": goal_text, "outcome": "DONE", "jid": "", "display_result": answer}]),
        encoding="utf-8")


def _assert_recovered(state, mech, text, source):
    goals, _ = _resume(state, _done(1))
    assert [g["subtask_index"] for g in goals] == [2], "a recovered child must not re-run"
    lines = [r for r in _ledger(state) if r.get("kind") == "child_result"]
    assert len(lines) == 1
    assert lines[0]["recovered"] is True and lines[0]["result"] == text
    assert lines[0]["subtask_index"] == 1 and lines[0]["source"] == source
    assert [r["extra"]["result"] for r in _mech_rows(mech)] == ["recovered"]


def test_recovered_from_the_outcome_file(tmp_path, mech):
    _write_rows(tmp_path, [_header(), _child(1), _child(2)])
    _outcome_file(tmp_path, "cDUR-1", "from outcome")
    _assert_recovered(tmp_path, mech, "from outcome", "outcome_json")


@pytest.mark.parametrize("gz", [False, True])
def test_recovered_from_the_worker_transcript(tmp_path, mech, gz):
    _write_rows(tmp_path, [_header(), _child(1), _child(2)])
    _transcript(tmp_path, "r1_a0_w0", _child(2)["text"], "other child", gz=gz)
    _transcript(tmp_path, "r1_a0_w1", _child(1)["text"], "the last answer", gz=gz)
    _assert_recovered(tmp_path, mech, "the last answer", "transcript")


def test_recovered_from_history(tmp_path, mech):
    _write_rows(tmp_path, [_header(), _child(1), _child(2)])
    _history(tmp_path, _child(1)["text"], "from history")
    _assert_recovered(tmp_path, mech, "from history", "history")


def test_the_outcome_file_wins_over_the_transcript(tmp_path, mech):
    _write_rows(tmp_path, [_header(), _child(1), _child(2)])
    _outcome_file(tmp_path, "cDUR-1", "from outcome")
    _transcript(tmp_path, "r1_a0_w0", _child(1)["text"], "from transcript")
    _assert_recovered(tmp_path, mech, "from outcome", "outcome_json")


def test_a_second_resume_does_not_duplicate_the_recovered_line(tmp_path, mech):
    _write_rows(tmp_path, [_header(), _child(1), _child(2)])
    _outcome_file(tmp_path, "cDUR-1", "from outcome")
    _resume(tmp_path, _done(1))
    _resume(tmp_path, _done(1))
    assert [r.get("kind") for r in _ledger(tmp_path)].count("child_result") == 1


# ---- 2. resume: not recoverable -> re-queued ONCE ------------------------------------------

def test_an_unrecoverable_child_is_requeued_once_and_leaves_a_mechanism_row(tmp_path, mech):
    _write_rows(tmp_path, [_header(), _child(1), _child(2)])
    goals, _ = _resume(tmp_path, _done(1))
    assert sorted(g["subtask_index"] for g in goals) == [1, 2]
    assert [r.get("kind") for r in _ledger(tmp_path)].count("child_requeued") == 1
    assert "requeued" in [r["extra"]["result"] for r in _mech_rows(mech)]
    # the re-queued run died again without leaving an answer: the cap is one
    goals2, _ = _resume(tmp_path, _done(1))
    assert [g["subtask_index"] for g in goals2] == [2]
    assert [r.get("kind") for r in _ledger(tmp_path)].count("child_requeued") == 1


# ---- 1. durability: the worker writes the line when the outcome is set ---------------------

def test_settling_done_calls_the_durable_writer_and_a_non_done_does_not():
    seen = []
    stub = types.SimpleNamespace(
        status="", outcome=None, on_settled_done=lambda w: seen.append(w.outcome),
        _record_tree_stability=lambda: None, _claim_verdict=lambda: "DONE")
    rf.RelayWorker._settle_done(stub)
    assert seen == ["DONE"]
    stub.on_settled_done = lambda w: seen.append("again")
    rf.RelayWorker._settle_done(stub, outcome_override="EVIDENCE_CONTRADICTED")
    assert seen == ["DONE"]
    stub.on_settled_done = lambda w: 1 / 0          # a failing writer never fails the worker
    rf.RelayWorker._settle_done(stub)
    assert stub.outcome == "DONE"


def _seam():
    spec = importlib.util.spec_from_file_location(
        "seam_helpers_dur", os.path.join(REPO, "relay",
                                         "test_the_recovered_family_reaches_a_merge.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_the_child_line_exists_at_the_moment_the_outcome_is_set(tmp_path, monkeypatch, mech):
    """Through run_relay_fleet: the worker settles via _settle_done and the line is on disk
    BEFORE poll returns, i.e. before any later sweep could have written it."""
    h = _seam()
    h._browserless(monkeypatch)
    at_settle = []

    def poll(self):
        if self.status in rf.TERMINAL:
            return True
        self.last_response = "child answer %s" % self.name
        self._settle_done(outcome_override="DONE")
        at_settle.append([r for r in _ledger(tmp_path) if r.get("kind") == "child_result"])
        return True

    monkeypatch.setattr(rf.RelayWorker, "poll", poll)
    tdir = h._crashed_run(tmp_path, h._header(n=2))
    h_goals = h._orphan_children(2)
    rf.run_relay_fleet(h._FakeContext(), h_goals, "http://agent", max_concurrent=1, poll_s=0,
                       transcript_dir=tdir, notify=lambda *a, **k: None, fanout=False)
    assert at_settle[0] and at_settle[0][0]["subtask_index"] == 1
    lines = [r for r in _ledger(tmp_path) if r.get("kind") == "child_result"]
    assert sorted(r["subtask_index"] for r in lines) == [1, 2], "no duplicate from the sweep"
    assert len([r for r in _ledger(tmp_path) if r.get("kind") == "merged"]) == 1


# ---- 3. stall detector ---------------------------------------------------------------------

def _stall_run(tmp_path, monkeypatch, rows, done):
    h = _seam()
    tdir = h._crashed_run(tmp_path, rows)
    (tmp_path / "last_run_done.json").write_text(json.dumps(done), encoding="utf-8")
    h._browserless(monkeypatch)
    res = rf.run_relay_fleet(h._FakeContext(), [], "http://agent", max_concurrent=2, poll_s=0,
                             transcript_dir=tdir, notify=lambda *a, **k: None, fanout=False)
    return h, res


def test_stall_detector_recovers_the_answer_and_the_family_merges_once(tmp_path, monkeypatch, mech):
    rows = [_header(), _child(1), _child(2), _result_line(1)]
    _outcome_file(tmp_path, "cDUR-2", "recovered at stall")
    h, res = _stall_run(tmp_path, monkeypatch, rows, _done(1, 2))
    assert len(h._aggregators(res)) == 1, "the family stayed unmerged"
    led = _ledger(tmp_path)
    rec = [r for r in led if r.get("kind") == "child_result" and r.get("recovered")]
    assert len(rec) == 1 and rec[0]["subtask_index"] == 2
    assert [r.get("kind") for r in led].count("merged") == 1
    kinds = [r["mechanism"] for r in _mech_rows(mech)]
    assert "merge_stalled_missing_child_result" in kinds


def test_stall_detector_requeues_an_unrecoverable_child_then_merges_once(tmp_path, monkeypatch, mech):
    rows = [_header(), _child(1), _child(2), _result_line(1)]
    h, res = _stall_run(tmp_path, monkeypatch, rows, _done(1, 2))
    led = _ledger(tmp_path)
    assert [r.get("kind") for r in led].count("child_requeued") == 1
    assert len(h._aggregators(res)) == 1
    assert [r.get("kind") for r in led].count("merged") == 1
    assert [r.get("subtask_index") for r in led if r.get("kind") == "child_result"
            and not r.get("recovered")].count(2) == 1, "the re-run child writes one line"


def test_a_merge_already_issued_is_not_issued_again_by_the_stall_path(tmp_path, monkeypatch, mech):
    rows = [_header(), _child(1), _child(2), _result_line(1),
            {"kind": "merged", "campaign_id": CID, "agg_key": "K"},
            {"kind": "merge_requeued", "campaign_id": CID, "attempt": 1}]
    h, res = _stall_run(tmp_path, monkeypatch, rows, _done(1, 2))
    assert h._aggregators(res) == []


def test_no_stall_when_the_missing_child_is_not_done(tmp_path, monkeypatch, mech):
    rows = [_header(), _child(1), _child(2), _result_line(1)]
    h, res = _stall_run(tmp_path, monkeypatch, rows, _done(1))
    assert h._aggregators(res) == []
    assert "merge_stalled_missing_child_result" not in [r["mechanism"] for r in _mech_rows(mech)]
