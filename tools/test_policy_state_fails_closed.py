"""Deleting one file used to turn the approval gate off.

`load_contract()` returned None for "no contract is active", for "the file is missing", and
for "the file is corrupt", and `check_op` treats None as inert. The file lives under .fleet,
which every worker can write. So a worker could disable the gate by removing it -- and this
repository already carries the rule that a denylist must fail closed, learned the same way.

The memory of "we saw an active contract" and "it was retired legitimately" is kept ON DISK,
not in a module global, so it holds across processes: the fleet-runner process retires a
contract and the MCP-server process that gates operations sees the same record.
"""
import json

import pytest

from tools import contract_gate as CG


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Isolate every destination the gate touches, not just the one under test.

    check_op's fail-closed path CREATES AN APPROVAL GATE, and _create_gate resolves its
    directory from tools.file_ops.ALLOWED_BASE -- which was once still the real one, so this
    file posted live approval requests into the operator's queue, one reading `detail=rm -rf
    /`, until the fixture also redirected ALLOWED_BASE. The seen/retired sidecars derive from
    _CONTRACT_FILE.parent, so pointing _CONTRACT_FILE at tmp_path isolates them too -- there
    is no module global left to leak into the next test.
    """
    import tools.file_ops as FO
    monkeypatch.setattr(CG, "_CONTRACT_FILE", tmp_path / "active_contract.json")
    monkeypatch.setattr(FO, "ALLOWED_BASE", tmp_path / "base")
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
    _write({"active": True, "ask_before": ["shell_destructive"], "started": 1719500000.0})
    assert CG.contract_state()[0] == "active"
    CG._CONTRACT_FILE.unlink()
    assert CG.contract_state()[0] == "absent"
    assert CG.policy_state_is_suspect() is not None, "absence after an active contract is tampering"
    assert CG.check_op("shell_destructive", "rm -rf /") is not None


def test_a_contract_retired_through_the_api_may_vanish():
    """Ending a run legitimately must not leave the machine permanently gating."""
    _write({"active": True, "ask_before": ["shell_destructive"], "started": 1719500000.0})
    CG.contract_state()
    CG.deactivate_contract()
    CG._CONTRACT_FILE.unlink()
    assert CG.policy_state_is_suspect() is None
    assert CG.check_op("shell_destructive", "rm -rf /") is None


def test_retirement_of_one_contract_does_not_excuse_deleting_another():
    """A retirement record names the contract it retired; a DIFFERENT contract that then
    disappears is still tampering. Otherwise a single legitimate retirement would forgive
    every future deletion."""
    _write({"active": True, "ask_before": ["shell_destructive"], "started": 1719500000.0})
    CG.contract_state()
    CG.deactivate_contract()          # retire contract #1
    CG._CONTRACT_FILE.unlink()
    # A new, different contract is activated and then deleted without being retired.
    _write({"active": True, "ask_before": ["shell_destructive"], "started": 1719600000.0})
    assert CG.contract_state()[0] == "active"
    CG._CONTRACT_FILE.unlink()
    assert CG.policy_state_is_suspect() is not None, \
        "an old retirement must not excuse deleting a newer active contract"
    assert CG.check_op("shell_destructive", "rm -rf /") is not None


def test_the_record_crosses_a_fresh_module_state():
    """The seen/retired memory is on disk, so a process that never called contract_state()
    while the contract was active still refuses -- simulated by clearing any in-process
    caches and reading only what is on the filesystem.

    This is the cross-process case that a module global could not cover: one process saw the
    contract, another must honour that it vanished.
    """
    _write({"active": True, "ask_before": ["shell_destructive"], "started": 1719500000.0})
    assert CG.contract_state()[0] == "active"   # process A observes it, writing the sidecar
    CG._CONTRACT_FILE.unlink()
    # A different process would not have the contract file but WOULD read the same sidecars.
    seen = json.loads((CG._CONTRACT_FILE.parent / "contract_seen.json").read_text("utf-8"))
    assert seen.get("identity"), "seen record must be persisted for another process to read"
    assert CG.policy_state_is_suspect() is not None


def test_the_untrusted_message_says_which_of_the_two_cases_it_is():
    """An operator who cannot tell 'corrupt' from 'deleted' cannot respond to either."""
    _write("{ nope")
    assert "could not be read" in CG.policy_state_is_suspect()
    # Simulate having seen an active contract, then have the file disappear entirely.
    _write({"active": True, "ask_before": [], "started": 1719500000.0})
    CG.contract_state()
    CG._CONTRACT_FILE.unlink()
    assert "disappeared" in CG.policy_state_is_suspect()


def test_load_contract_still_answers_the_old_question():
    """Existing callers that only want the object keep working."""
    _write({"active": True, "ask_before": []})
    assert CG.load_contract() == {"active": True, "ask_before": []}
    CG._CONTRACT_FILE.unlink()
    assert CG.load_contract() is None


def test_the_fail_closed_path_writes_its_gate_away_from_the_operator(tmp_path):
    """The gate goes where the CODE resolves it, and that is not the operator's live directory.

    THIS GUARD ALREADY EXISTED FOR THIS EXACT INCIDENT and was green while it happened again.
    It used to redirect `tools.file_ops.ALLOWED_BASE` in its own fixture and assert the gate
    landed under it -- true, and true only of this file. Every other test raises its gate
    through the same writer with the real ALLOWED_BASE, and this assertion said nothing about
    them: a per-file guard against a repo-wide leak. Measured 2026-09-12: seven approval gates
    from this repository's own tests reached the operator's screen, and he pressed 承認 on one.

    The repo-wide guard was already there -- conftest sets MCP_GATE_DIR at module scope, before
    tools.gate_ops is imported -- and contract_gate built its path by hand and never read it.

    BOTH HALVES ARE NEEDED. "It went where the resolver said" is vacuous if the resolver points
    at the real directory, and "it is not the real directory" is vacuous if nothing checks the
    gate arrived at all.
    """
    import os
    _write("{ not json")
    assert CG.check_op("shell_destructive", "rm -rf /") is not None

    gate_dir = CG._gate_dir()
    assert gate_dir.is_dir(), "the fail-closed path did not create a gate directory"
    token = CG._stable_token("shell_destructive", "rm -rf /")
    assert (gate_dir / ("%s.json" % token)).is_file(), (
        "the fail-closed path did not create a gate at all")

    live = os.path.realpath(os.path.join(os.path.expanduser("~"), ".companion_gates"))
    assert os.path.realpath(str(gate_dir)) != live, (
        "a test wrote an approval gate into the operator's live directory (%s) -- this is the "
        "leak that put `rm -rf /` on his screen" % gate_dir)
