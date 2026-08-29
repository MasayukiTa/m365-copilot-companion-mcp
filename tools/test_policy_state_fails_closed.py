"""Deleting one file used to turn the approval gate off.

`load_contract()` returned None for "no contract is active", for "the file is missing", and
for "the file is corrupt", and `check_op` treats None as inert. The file lives under .fleet,
which every worker can write. So a worker could disable the gate by removing it -- and this
repository already carries the rule that a denylist must fail closed, learned the same way.

A worker can write files. It cannot write this process's memory, which is the whole basis of
the fix: the server remembers that it saw an active contract.
"""
import json

import pytest

from tools import contract_gate as CG


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(CG, "_CONTRACT_FILE", tmp_path / "active_contract.json")
    monkeypatch.setattr(CG, "_SEEN", {"active_contract": False, "retired_via_api": False})
    yield


def _write(obj_or_text):
    p = CG._CONTRACT_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(obj_or_text if isinstance(obj_or_text, str)
                 else json.dumps(obj_or_text), encoding="utf-8")


def test_absent_from_the_start_is_still_inert():
    """A machine with no contract at all is the ordinary case and must not start gating."""
    assert CG.contract_state()[0] == "absent"
    assert CG.policy_state_is_suspect() is None
    assert CG.check_op("shell_destructive", "rm -rf /") is None


def test_a_corrupt_policy_file_is_not_inert():
    """Present and unparseable is not 'no policy'. It was."""
    _write("{ this is not json")
    assert CG.contract_state()[0] == "unreadable"
    assert CG.policy_state_is_suspect() is not None
    assert CG.check_op("shell_destructive", "rm -rf /") is not None


def test_a_policy_that_is_not_an_object_is_not_inert():
    _write("[1, 2, 3]")
    assert CG.contract_state()[0] == "unreadable"
    assert CG.check_op("shell_destructive", "x") is not None


def test_deleting_an_active_contract_does_not_disable_the_gate():
    """THE DEFECT, end to end: see an active contract, delete the file, try a gated op."""
    _write({"active": True, "ask_before": ["shell_destructive"]})
    assert CG.contract_state()[0] == "active"
    CG._CONTRACT_FILE.unlink()
    assert CG.contract_state()[0] == "absent"
    assert CG.policy_state_is_suspect() is not None, "absence after an active contract is tampering"
    assert CG.check_op("shell_destructive", "rm -rf /") is not None


def test_a_contract_retired_through_the_api_may_vanish():
    """Ending a run legitimately must not leave the machine permanently gating."""
    _write({"active": True, "ask_before": ["shell_destructive"]})
    CG.contract_state()
    CG.deactivate_contract()
    CG._CONTRACT_FILE.unlink()
    assert CG.policy_state_is_suspect() is None
    assert CG.check_op("shell_destructive", "rm -rf /") is None


def test_the_untrusted_message_says_which_of_the_two_cases_it_is():
    """An operator who cannot tell 'corrupt' from 'deleted' cannot respond to either."""
    _write("{ nope")
    assert "could not be read" in CG.policy_state_is_suspect()
    CG._SEEN["active_contract"] = True
    CG._CONTRACT_FILE.unlink()
    assert "disappeared" in CG.policy_state_is_suspect()


def test_load_contract_still_answers_the_old_question():
    """Existing callers that only want the object keep working."""
    _write({"active": True, "ask_before": []})
    assert CG.load_contract() == {"active": True, "ask_before": []}
    CG._CONTRACT_FILE.unlink()
    CG._SEEN["retired_via_api"] = True
    assert CG.load_contract() is None
