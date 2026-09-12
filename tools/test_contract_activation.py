"""activate_contract() -- the missing half of deactivate_contract() (codex-plan item 3,
2026-09-08's own plan, executed 2026-09-09).

Nothing in this codebase could turn a contract ON before this. deactivate_contract() existed,
tested, called by fleet_runner on exit -- but the thing it deactivates had no first-class way
to be activated at all; the module docstring's own schema for active_contract.json was
documentation of a shape nobody wrote, activation done (if at all) by hand-editing the file to
the documented shape. This file tests the write path the same way test_policy_state_fails_
closed.py already tests the read path, with the same isolation fixture for the same reason:
check_op's fail-closed path creates a real approval gate under tools.file_ops.ALLOWED_BASE, and
that must never be the operator's real queue during a test.
"""
import json

import pytest

from tools import contract_gate as CG


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    import tools.file_ops as FO
    monkeypatch.setattr(CG, "_CONTRACT_FILE", tmp_path / "active_contract.json")
    monkeypatch.setattr(FO, "ALLOWED_BASE", tmp_path / "base")
    yield


def test_activation_writes_an_active_contract():
    out = CG.activate_contract(scope="C:/demo", ask_before=["delete"])
    assert out["ok"] is True
    assert CG.contract_state()[0] == "active"
    assert CG.load_contract()["ask_before"] == ["delete"]
    assert CG.load_contract()["scope"] == "C:/demo"


def test_activation_records_a_real_started_epoch():
    """_contract_identity() keys on `started`; a contract without one falls back to a content
    hash, which would make two identical activations indistinguishable. activate_contract must
    always mint a fresh, real epoch."""
    before = __import__("time").time()
    out = CG.activate_contract(ask_before=["delete"])
    after = __import__("time").time()
    started = out["contract"]["started"]
    assert before <= started <= after


def test_an_unknown_op_class_is_refused_not_silently_written():
    """The exact failure this validation exists to prevent: a typo'd op_class would write
    successfully and gate nothing, forever, silently."""
    out = CG.activate_contract(ask_before=["delet"])  # typo
    assert out["ok"] is False
    assert "delet" in out["detail"]
    assert CG.contract_state()[0] == "absent", "the bad write must not have landed at all"


def test_activating_over_an_already_active_contract_is_refused():
    """Two activations in a row would silently replace the first contract's identity -- and
    whoever holds a retirement obligation for the first one would then be unable to discharge
    it, because deactivate_contract() would retire the SECOND contract's identity instead."""
    first = CG.activate_contract(ask_before=["delete"])
    assert first["ok"] is True
    second = CG.activate_contract(ask_before=["outbound"])
    assert second["ok"] is False
    assert CG.load_contract()["ask_before"] == ["delete"], (
        "the second activation overwrote the first despite being refused")


def test_deactivate_after_activate_leaves_a_clean_retirement_no_suspicion():
    """The full round trip, and the property that matters most: after a legitimate
    activate -> deactivate cycle, policy_state_is_suspect() must read None. A contract that
    leaves the machine looking tampered after a NORMAL, successful use would be worse than
    the defect this module exists to prevent."""
    CG.activate_contract(ask_before=["delete"])
    assert CG.check_op("delete", "demo") is not None, "the gate did not fire while active"
    CG.deactivate_contract()
    assert CG.contract_state()[0] in ("absent", "inactive")
    assert CG.policy_state_is_suspect() is None, (
        "a clean, legitimate deactivation was read as tampering")
    assert CG.check_op("delete", "demo") is None, "the gate kept firing after deactivation"


def test_activate_then_approve_then_pass_is_the_full_hitl_cycle():
    """The plan's own evidence bar names three states, not one: an ask_before op with no
    answer yet is refused; the SAME op, once approved, passes; an op outside ask_before never
    gates at all. This is all three, driven through the real check_op/gate-file mechanism a
    human operator actually uses -- not asserted against internals."""
    CG.activate_contract(ask_before=["delete"])
    try:
        # (i) not yet answered -> refused/pending
        first = CG.check_op("delete", "demo scratch file")
        assert first is not None

        # find the gate file check_op just created and answer it the way a human would.
        # ASK THE CODE WHERE IT PUT IT. This rebuilt ALLOWED_BASE/".companion_gates" itself,
        # which was true of _create_gate at the time and was exactly the defect: conftest sets
        # MCP_GATE_DIR at module scope so that no test can write into the operator's live gate
        # directory, and contract_gate built its path by hand and never read it. Measured
        # 2026-09-12: seven approval gates from this repository's own tests reached the
        # operator's screen.
        gates_dir = CG._gate_dir()
        assert gates_dir.is_dir(), "check_op did not create a gate directory"

        # BY TOKEN, NOT BY COUNT. MCP_GATE_DIR is set once per pytest process and shared by
        # every test in the run, so `len(...) == 1` passed only while this was the sole test
        # raising a gate -- and then failed in this file when another one did. Gates are keyed
        # by token exactly so a caller can find its own among others'; this does what
        # check_op's caller does.
        token = CG._stable_token("delete", "demo scratch file")
        gate_file = gates_dir / ("%s.json" % token)
        assert gate_file.is_file(), (
            "check_op did not create the gate for its own token (%s)" % token)
        gate = json.loads(gate_file.read_text(encoding="utf-8"))
        gate["answered"] = True
        gate["answer"] = "approved"
        gate_file.write_text(json.dumps(gate, ensure_ascii=False), encoding="utf-8")

        # (ii) the SAME op, now approved -> passes
        second = CG.check_op("delete", "demo scratch file")
        assert second is None, "an approved operation was still refused"

        # (iii) an op_class never listed in ask_before -> never gated, and no gate of ITS
        # own appears. Asked by token for the same reason as above: a count over a shared
        # directory answers a question about the whole run, not about this call.
        unlisted = CG._stable_token("outbound", "demo email nobody approved")
        third = CG.check_op("outbound", "demo email nobody approved")
        assert third is None, "an unlisted op_class was gated anyway"
        assert not (gates_dir / ("%s.json" % unlisted)).is_file(), (
            "an unlisted op_class created a gate file")
    finally:
        CG.deactivate_contract()
