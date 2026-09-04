# -*- coding: utf-8 -*-
"""Checking a DONE claim against the record, in the FLEET and not only in the benchmark.

THE GAP THIS CLOSES. `outcome == DONE` is a self-report, measured at precision 0.718 -- 11 of
39 claims wrong on a 40-instance slice. A whole verification pipeline was built for exactly
that number and then wired only into bench/pro_cycle.py, so ordinary fleet runs went on
reporting an unchecked self-report. The machinery existed and did not run where it mattered.

CONSERVATIVE BY CONSTRUCTION. Only a positive contradiction changes anything. No ledger data,
no acceptance checks, or any other verdict all leave DONE exactly as it was: an absence of
evidence is not evidence, and a worker must not be demoted because nothing was recording.
"""
import pytest

from relay import relay_fleet as RF


class W:
    """Only what _claim_verdict reads. A real RelayWorker needs a browser context; the
    decision is the part that has to be right."""
    VERIFY_CLAIM_AGAINST_LEDGER = True
    _claim_verdict = RF.RelayWorker._claim_verdict
    _settle_done = RF.RelayWorker._settle_done

    def __init__(self, checks=None, cwd="C:/w/task1"):
        self.checks = checks
        self.cwd = cwd
        self.status = ""
        self.outcome = ""


def ledger(monkeypatch, events):
    from tools import tool_ledger as tl
    monkeypatch.setattr(tl, "for_task", lambda task, rows=None, root="": events)


CHECKS = [{"id": "tests", "cmd": "pytest -x"}]


def call(tool, args):
    return {"call": {"tool": tool, "args": {k: {"text": v} for k, v in args.items()}},
            "outcome": {"ok": True}}


# -- the one case that changes anything ----------------------------------------------------

def test_a_contradicted_claim_is_not_reported_as_done(monkeypatch):
    """The worker never ran the command its own contract names, and said DONE."""
    ledger(monkeypatch, [call("write_file", {"path": "C:/w/task1/a.py"})])
    assert W(CHECKS)._claim_verdict() == "EVIDENCE_CONTRADICTED"


def test_the_status_stays_done_so_the_run_still_terminates(monkeypatch):
    """A new STATUS would strand the card: the cockpit's terminal-status list is hand-written,
    and an unknown one there once stopped archiving for a whole run. The outcome carries the
    finding; the status stays a value every existing path already handles."""
    ledger(monkeypatch, [call("write_file", {"path": "C:/w/task1/a.py"})])
    w = W(CHECKS)
    w._settle_done()
    assert w.status == "done"
    assert w.outcome == "EVIDENCE_CONTRADICTED"


# -- everything else must be left alone ----------------------------------------------------

def test_a_worker_that_wrote_and_then_ran_its_check_stays_done(monkeypatch):
    """BOTH are required, and the first draft of this test only had the second. A worker that
    runs the acceptance command having changed nothing is contradicted too -- it claimed DONE
    without doing anything -- and the assessment was right where the test was not."""
    ledger(monkeypatch, [
        call("write_file", {"path": "C:/w/task1/a.py"}),
        call("shell_exec", {"command": "pytest -x", "working_dir": "C:/w/task1"}),
    ])
    assert W(CHECKS)._claim_verdict() == "DONE"


def test_an_exec_only_record_is_not_treated_as_a_contradiction(monkeypatch):
    """THIS TEST ASSERTED THE OPPOSITE, and the behaviour it pinned was measured wrong.

    It demanded EVIDENCE_CONTRADICTED for a worker that ran its acceptance command without
    calling any write tool -- "it claimed DONE without doing anything". Then
    evidence_manifest was corrected against real data: nine instances were told nothing in the
    workspace had changed, all nine had exec calls, and FOUR of them had produced a patch that
    graded RESOLVED. run_python and shell_exec can write anything, and no list of tool names
    will ever see them do it.

    So the record cannot settle this case, and the honest verdict is UNVERIFIABLE, which
    _claim_verdict deliberately leaves as DONE -- an absence of evidence is not evidence, and a
    worker must not be demoted because the ledger has a blind spot. The test survived the
    correction unchanged and failed on every CI run afterwards, still asking for a demotion the
    data had already refuted."""
    ledger(monkeypatch, [call("shell_exec", {"command": "pytest -x", "working_dir": "C:/w/task1"})])
    assert W(CHECKS)._claim_verdict() == "DONE"


def test_the_blind_spot_is_reported_rather_than_hidden(monkeypatch):
    """Not demoting is not the same as saying nothing. The assessor has to name why it cannot
    tell, or the next reader re-derives it from scratch."""
    from relay import evidence_manifest as EM
    events = [{"call": {"tool": "shell_exec", "ts": 1.0,
                        "args": {"command": "pytest -x", "working_dir": "C:/w/task1"}},
               "outcome": {"ok": True}}]
    got = EM.assess(True, {"checks": [{"id": "tests", "command": "pytest -x"}]}, events)
    assert got["verdict"] == EM.UNVERIFIABLE
    assert any("cannot see" in r for r in got["reasons"]), got["reasons"]


def test_no_recorded_calls_leaves_done_untouched(monkeypatch):
    """AN ABSENCE OF EVIDENCE IS NOT EVIDENCE. The ledger was empty for a whole night because
    the server was running older code; demoting every worker for that would have been wrong."""
    ledger(monkeypatch, [])
    assert W(CHECKS)._claim_verdict() == "DONE"


def test_a_task_with_no_checks_is_not_demoted(monkeypatch):
    """Most fleet goals carry no mechanical check. They are unverified, which is a fact about
    the goal, not a failure by the worker."""
    ledger(monkeypatch, [call("write_file", {"path": "C:/w/task1/a.py"})])
    assert W(None)._claim_verdict() == "DONE"
    assert W([])._claim_verdict() == "DONE"


def test_a_check_with_no_command_is_not_a_contract(monkeypatch):
    ledger(monkeypatch, [call("write_file", {"path": "C:/w/task1/a.py"})])
    assert W([{"id": "x"}])._claim_verdict() == "DONE"


def test_no_working_directory_means_no_attribution(monkeypatch):
    """Calls are attributed by the path they operated on. With no cwd there is nothing to
    match, and matching nothing must not read as contradicting."""
    ledger(monkeypatch, [call("write_file", {"path": "C:/w/task1/a.py"})])
    assert W(CHECKS, cwd="")._claim_verdict() == "DONE"


def test_an_exploding_ledger_cannot_fail_the_worker(monkeypatch):
    from tools import tool_ledger as tl
    monkeypatch.setattr(tl, "for_task",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert W(CHECKS)._claim_verdict() == "DONE"


def test_the_switch_turns_it_off(monkeypatch):
    ledger(monkeypatch, [call("write_file", {"path": "C:/w/task1/a.py"})])
    w = W(CHECKS)
    w.VERIFY_CLAIM_AGAINST_LEDGER = False
    assert w._claim_verdict() == "DONE"


def test_it_is_on_by_default():
    """Built for a measured 0.718 and then left switched off would be the same defect in a
    different place."""
    assert RF.RelayWorker.VERIFY_CLAIM_AGAINST_LEDGER is True


# -- the routes around it ------------------------------------------------------------------

def test_there_is_exactly_one_place_this_worker_becomes_done():
    """There were FOUR sites assigning ("done", "DONE"). Adding the check at one would have
    left three routes around it, which is how a gate comes to protect nothing."""
    import inspect
    src = inspect.getsource(RF)
    assert 'self.status, self.outcome = "done", "DONE"' not in src, \
        "a DONE assignment bypasses _settle_done"
    assert src.count("def _settle_done") == 1
