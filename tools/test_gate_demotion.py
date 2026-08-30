"""The regex gate becomes an audit signal only once its replacement is switched on.

Both reviews said the same thing about these patterns: a denylist over command text is the
wrong shape for containment and should end up as a warning. The reason to wait was concrete --
it is the only control there is while a worker can still execute on this machine, so removing
it before the replacement is live opens a gap rather than closing one.
"""
import io
import json
import os

import pytest

from tools import contract_gate as CG


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    import tools.file_ops as FO
    monkeypatch.setattr(CG, "_CONTRACT_FILE", tmp_path / "active_contract.json")
    monkeypatch.setattr(CG, "_FLEET_DIR", tmp_path)
    monkeypatch.setattr(CG, "_SEEN", {"active_contract": False, "retired_via_api": False})
    monkeypatch.setattr(FO, "ALLOWED_BASE", tmp_path / "base")
    yield


def _active_contract(tmp_path):
    (tmp_path / "active_contract.json").write_text(
        json.dumps({"active": True, "ask_before": ["shell_destructive"]}), encoding="utf-8")


def test_it_still_gates_while_execution_can_happen_here(tmp_path, monkeypatch):
    """Routing off means a worker can still run the matched command on this machine, and the
    denylist is the only thing in the way."""
    monkeypatch.delenv("SWE_BROKER", raising=False)
    _active_contract(tmp_path)
    assert CG.check_op("shell_destructive", "rm -rf /") is not None


def test_it_demotes_to_an_audit_record_once_execution_is_routed(tmp_path, monkeypatch):
    """With execution in a container on another host, the matched command cannot run here.
    Holding it for approval asks a person to approve something that was never going to
    happen, which is how an approval queue becomes noise people click through."""
    monkeypatch.setenv("SWE_BROKER", "on")
    _active_contract(tmp_path)
    assert CG.check_op("shell_destructive", "rm -rf /") is None
    audit = tmp_path / "gate_audit.jsonl"
    assert audit.is_file(), "the match must still be recorded, it just stops being a question"
    row = json.loads(io.open(audit, encoding="utf-8").read().strip().splitlines()[-1])
    assert row["op_class"] == "shell_destructive" and row["mode"] == "audit"
    assert "routed" in row["why"]


def test_the_demotion_is_conditional_not_a_deletion():
    """A demotion written as 'delete the patterns' cannot come back when routing is off."""
    import inspect
    src = inspect.getsource(CG.check_op)
    assert "_bc.enabled()" in src
    assert "return None" in src
    assert len(CG._DESTRUCTIVE_PATTERNS) > 30, "the patterns must still exist"


def test_an_audit_write_that_fails_does_not_change_the_answer(tmp_path, monkeypatch):
    """Bookkeeping must never decide whether an operation proceeds."""
    monkeypatch.setenv("SWE_BROKER", "on")
    monkeypatch.setattr(CG, "_FLEET_DIR", tmp_path / "does" / "not" / "exist")
    _active_contract(tmp_path)
    assert CG.check_op("shell_destructive", "rm -rf /") is None
