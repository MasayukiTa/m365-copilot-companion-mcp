# -*- coding: utf-8 -*-
"""Regression for job bfa6b22fac93: a one-turn DONE with "judge: none" on a goal that only
POINTS at a request file.

The real goal was an absolute Downloads path to the case folder followed by 「これ調べて」. Two
things went wrong and both are pinned here:
  (1) case_reference_search did not recognise the full-path shape, so the request file was never
      read and is_data_analysis_goal classified the goal (text only) as non-analysis. After the
      fix the resolved request text (which names ADAMS) classifies it as data analysis.
  (2) nothing stopped a DONE that never opened the resolved request file. _settle_done now
      refuses a DONE when a request_path was resolved but no content-reading tool ran.
"""
import pytest
from relay.relay_fleet import RelayWorker
from relay import judge_gate
from tools import search_ops

CASE_NAME = "枝さん_679かすれ調査"


def _full_path_goal(tmp_path):
    return str(tmp_path / CASE_NAME) + "\u3000これ調べて"


def _make_case(tmp_path, body):
    case = tmp_path / CASE_NAME
    case.mkdir()
    (case / "やりたいこと.txt").write_text(body, encoding="utf-8")
    return case


def test_full_path_goal_is_classified_by_the_request_file_content(tmp_path, monkeypatch):
    # The goal text alone has no data-analysis marker; the request file names ADAMS. The
    # resolved request must make is_data_analysis_goal true.
    _make_case(tmp_path, "ADAMSデータを参照してかすれの原因を調べる")
    real = search_ops.case_reference_search
    monkeypatch.setattr(search_ops, "case_reference_search",
                        lambda goal: real(goal, [tmp_path]))
    goal = _full_path_goal(tmp_path)
    worker = RelayWorker(goal, "fullpath-test")
    # Identity preserved; request file content present in the prompt and in the judge input.
    assert worker.goal == goal
    assert "ADAMS" in worker._judge_request_text
    assert judge_gate.is_data_analysis_goal(worker.goal, worker._judge_request_text)
    # The composed goal carries the resolved request path (so the worker is told to read it).
    assert str(tmp_path / CASE_NAME / "やりたいこと.txt") in worker.job


def test_done_is_refused_when_resolved_request_file_was_never_read(tmp_path, monkeypatch):
    _make_case(tmp_path, "ADAMSデータを参照")
    real = search_ops.case_reference_search
    monkeypatch.setattr(search_ops, "case_reference_search",
                        lambda goal: real(goal, [tmp_path]))
    worker = RelayWorker(_full_path_goal(tmp_path), "unread-test")
    assert worker._review_case["request_path"]
    # The worker did SOMETHING (a tool ran) but never a content-reading tool: the one-turn
    # no-op DONE shape of the real job.
    monkeypatch.setattr(RelayWorker, "_judge_tool_names", lambda self: {"list_directory"})
    worker._settle_done()
    assert worker.outcome == "VERIFY_FAILED"
    assert worker.status == "stuck"
    assert "never read" in worker.reason


def test_done_proceeds_when_the_request_file_was_read(tmp_path, monkeypatch):
    _make_case(tmp_path, "ADAMSデータを参照")
    real = search_ops.case_reference_search
    monkeypatch.setattr(search_ops, "case_reference_search",
                        lambda goal: real(goal, [tmp_path]))
    worker = RelayWorker(_full_path_goal(tmp_path), "read-test")
    # A content-reading tool ran: the unread gate must NOT fire.
    monkeypatch.setattr(RelayWorker, "_judge_tool_names",
                        lambda self: {"read_file", "odbc_query"})
    assert worker._case_file_unread() is False


def test_unread_gate_does_not_fire_without_a_resolved_case(tmp_path, monkeypatch):
    # No case reference at all -> no request_path -> the gate is inert (ordinary goals).
    monkeypatch.setattr(search_ops, "case_reference_search", lambda goal: None)
    worker = RelayWorker("implement the task in relay/foo.py", "plain-test")
    monkeypatch.setattr(RelayWorker, "_judge_tool_names", lambda self: {"list_directory"})
    assert worker._case_file_unread() is False


def test_unread_gate_is_inert_when_ledger_cannot_attribute(tmp_path, monkeypatch):
    # Empty attribution set AND no live attribution means the ledger cannot say -- a measurement
    # that cannot see must not fail the work. The gate must not fire on genuine no-evidence.
    _make_case(tmp_path, "ADAMS")
    real = search_ops.case_reference_search
    monkeypatch.setattr(search_ops, "case_reference_search",
                        lambda goal: real(goal, [tmp_path]))
    worker = RelayWorker(_full_path_goal(tmp_path), "noledger-test")
    monkeypatch.setattr(RelayWorker, "_judge_tool_names", lambda self: set())
    # The ledger is blind to this worker: attribution is not live.
    monkeypatch.setattr(RelayWorker, "_attribution_is_live", lambda self: False)
    assert worker._case_file_unread() is False


def test_unread_gate_fires_on_a_true_no_op_when_attribution_is_live(tmp_path, monkeypatch):
    # THE PRODUCTION no-op (job 065d8c1c4a14): the worker called NO tool at all, so its
    # attributed set is empty -- but the ledger IS recording and this worker's session IS bound
    # (attribution_is_live True). An empty slice then means "this worker ran nothing", which for
    # a resolved case reference proves the request file was never read. The gate MUST fire.
    _make_case(tmp_path, "ADAMS")
    real = search_ops.case_reference_search
    monkeypatch.setattr(search_ops, "case_reference_search",
                        lambda goal: real(goal, [tmp_path]))
    worker = RelayWorker(_full_path_goal(tmp_path), "noop-test")
    assert worker._review_case["request_path"]
    monkeypatch.setattr(RelayWorker, "_judge_tool_names", lambda self: set())
    monkeypatch.setattr(RelayWorker, "_attribution_is_live", lambda self: True)
    assert worker._case_file_unread() is True


def test_no_op_done_is_refused_end_to_end_when_attribution_is_live(tmp_path, monkeypatch):
    # The same production no-op, driven through _settle_done: a DONE that ran no tool at all on a
    # resolved case reference must be refused, not reported DONE.
    _make_case(tmp_path, "ADAMSデータを参照")
    real = search_ops.case_reference_search
    monkeypatch.setattr(search_ops, "case_reference_search",
                        lambda goal: real(goal, [tmp_path]))
    worker = RelayWorker(_full_path_goal(tmp_path), "noop-settle-test")
    monkeypatch.setattr(RelayWorker, "_judge_tool_names", lambda self: set())
    monkeypatch.setattr(RelayWorker, "_attribution_is_live", lambda self: True)
    worker._settle_done()
    assert worker.outcome == "VERIFY_FAILED"
    assert worker.status == "stuck"
    assert "never read" in worker.reason
