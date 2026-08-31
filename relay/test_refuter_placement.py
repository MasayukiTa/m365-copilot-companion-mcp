# -*- coding: utf-8 -*-
"""Where the refuter sits, and what it is allowed to skip.

WHY THIS MOVED. The refuter costs a model turn on the same tenant quota the work uses, and that
quota is the binding constraint: 217 of 237 turns refused in one measured run, with refusals
concentrated where the fleet was densest (median 35 concurrent replies at a refusal against 5 at
a recovery). Spending one on a claim the deterministic checks already settled spends the scarce
thing on a question that was free to answer.

WHAT IT MUST STILL BE ASKED ABOUT. Everything the machine cannot reach: a patch that passes and
misses the point, a requirement misread, a fix narrower than the issue. A task with NO
mechanical check is that case by definition, so "nothing to run" must never read as "nothing to
worry about" -- which is the failure the whole verification pipeline exists to stop.

OFF BY DEFAULT while the shadow numbers are collected, because "we stopped asking" and "there
was nothing to ask about" are indistinguishable in a pass rate.
"""
import pytest

from relay import relay_fleet as RF


class FakeWorker:
    """Only the attributes _deterministically_settled reads. Constructing a real RelayWorker
    needs a browser context; the decision is what must be right."""
    SKIP_REFUTER_WHEN_SETTLED = True
    _deterministically_settled = RF.RelayWorker._deterministically_settled

    def __init__(self, checks, verified):
        self.checks = checks
        self.verified = verified


def test_it_is_off_unless_asked_for(monkeypatch):
    """A behaviour that saves model turns by asking fewer questions must be switched on
    deliberately, and measured, not inherited by an upgrade."""
    w = FakeWorker(checks=[{"cmd": "pytest"}], verified=True)
    w.SKIP_REFUTER_WHEN_SETTLED = False
    assert w._deterministically_settled() is False


def test_checks_that_all_passed_settle_it():
    w = FakeWorker(checks=[{"cmd": "pytest"}], verified=True)
    assert w._deterministically_settled() is True


def test_a_task_with_no_checks_is_never_settled():
    """THE CASE THE REFUTER IS FOR. No mechanical oracle is precisely when a human-shaped
    review is worth a turn -- treating it as 'nothing to worry about' is the inversion."""
    assert FakeWorker(checks=[], verified=False)._deterministically_settled() is False
    assert FakeWorker(checks=None, verified=False)._deterministically_settled() is False


def test_checks_that_did_not_all_pass_are_not_settled():
    assert FakeWorker(checks=[{"cmd": "pytest"}], verified=False)._deterministically_settled() is False


def test_verified_without_checks_still_is_not_settled():
    """Defence against a later caller setting `verified` without anything having run."""
    assert FakeWorker(checks=[], verified=True)._deterministically_settled() is False


def test_it_never_raises():
    class Broken(FakeWorker):
        @property
        def checks(self):
            raise RuntimeError("boom")
    w = Broken.__new__(Broken)
    w.SKIP_REFUTER_WHEN_SETTLED = True
    assert w._deterministically_settled() is False


def test_the_env_switch_reads_as_off_for_anything_unrecognised(monkeypatch):
    for value in ("", "0", "no", "off", "maybe"):
        monkeypatch.setenv("MCP_REFUTER_SKIP_WHEN_SETTLED", value)
        import importlib
        importlib.reload(RF)
        assert RF.RelayWorker.SKIP_REFUTER_WHEN_SETTLED is False, value
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv("MCP_REFUTER_SKIP_WHEN_SETTLED", value)
        import importlib
        importlib.reload(RF)
        assert RF.RelayWorker.SKIP_REFUTER_WHEN_SETTLED is True, value
    monkeypatch.delenv("MCP_REFUTER_SKIP_WHEN_SETTLED", raising=False)
    import importlib
    importlib.reload(RF)
