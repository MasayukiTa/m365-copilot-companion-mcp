# -*- coding: utf-8 -*-
"""A contract can finally carry the turn budget the launcher already reads.

THE DEFECT, found while verifying codex-plan item 3 end to end. relay/relay_fleet.py reads
`budget_turns` off the active contract at fleet launch and tightens every worker's cap:

    if _c is not None and _c.get("active") and isinstance(_c.get("budget_turns"), int) \\
            and _c["budget_turns"] > 0:
        _contract_budget = _c["budget_turns"]
    effective_max_turns = (min(max_turns, _contract_budget) ...)

relay_fleet.py:2213 even carries a budget-specific stop reason for it. And `budget_turns`
appeared NOWHERE in tools/contract_gate.py, so activate_contract() -- the only function that can
write a contract at all -- could never set it. The branch was unreachable no matter how carefully
it had been written: the plan's own dominant failure shape, "implemented but never fires", sitting
inside the item meant to fix that shape.

The launcher's exact predicate is reproduced here on purpose. A test that only checked "the field
is in the file" would pass for budget_turns="3" or 0, which the launcher silently ignores -- and
a contract that looks like it set a budget and enforces nothing is worse than one that never
tried.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tools import contract_gate as CG  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Never touch the live .fleet/active_contract.json, and never the user-global kill switch.

    The second half is not hypothetical: verifying item 3 in a git worktree left the LIVE tree's
    stop switch engaged for four minutes, because GATE_DIR is `MCP_GATE_DIR or (ALLOWED_BASE /
    ".companion_gates")` and ALLOWED_BASE is the user's home -- identical in every worktree. A
    copy of the repo isolates .fleet and nothing else.
    """
    fleet = tmp_path / ".fleet"
    fleet.mkdir()
    monkeypatch.setattr(CG, "_FLEET_DIR", fleet, raising=False)
    monkeypatch.setattr(CG, "_CONTRACT_FILE", fleet / "active_contract.json", raising=False)
    monkeypatch.setenv("MCP_GATE_DIR", str(tmp_path / ".isolated_gates"))
    yield


def _written():
    return json.loads(CG._CONTRACT_FILE.read_text(encoding="utf-8"))


def _launcher_would_tighten(contract, max_turns):
    """relay/relay_fleet.py:5397-5410, reproduced exactly -- including what it IGNORES."""
    budget = None
    if (contract is not None and contract.get("active")
            and isinstance(contract.get("budget_turns"), int)
            and contract["budget_turns"] > 0):
        budget = contract["budget_turns"]
    return min(max_turns, budget) if budget is not None else max_turns


def test_a_budget_reaches_the_launcher_in_the_form_it_reads():
    out = CG.activate_contract(scope="budget test", stop_when=("delete",), budget_turns=3)
    assert out["ok"] is True, out
    written = _written()
    assert written["budget_turns"] == 3
    assert _launcher_would_tighten(written, max_turns=20) == 3, (
        "the launcher would have ignored this; the field is present but not in the form its "
        "predicate accepts")


def test_no_budget_leaves_the_field_out_entirely():
    """None must not become 0 or null in the file: the launcher's own comment says no contract
    and no budget both mean effective_max_turns == max_turns, and a key that is there but unusable
    invites a reader to think a budget was chosen."""
    assert CG.activate_contract(scope="no budget", stop_when=("delete",))["ok"] is True
    written = _written()
    assert "budget_turns" not in written, written
    assert _launcher_would_tighten(written, max_turns=20) == 20


def test_a_budget_larger_than_the_cap_does_not_raise_the_cap():
    """min(), not assignment. A contract is a tightening instrument; it must never be able to
    hand a worker MORE turns than the run was launched with."""
    assert CG.activate_contract(stop_when=("delete",), budget_turns=999)["ok"] is True
    assert _launcher_would_tighten(_written(), max_turns=20) == 20


@pytest.mark.parametrize("bad", [0, -1, "3", 3.0, True, False])
def test_a_budget_the_launcher_would_ignore_is_refused_at_the_door(bad):
    """The launcher ignores anything that is not a positive int, silently. Writing one anyway
    would produce a contract that reads as budgeted and enforces nothing -- the same silent
    no-op the op_class validation already exists to prevent.

    True is in this list deliberately: isinstance(True, int) is True in Python, so a bool would
    sail through the launcher's check as a budget of one turn, which is not what passing True
    could possibly have meant.
    """
    out = CG.activate_contract(stop_when=("delete",), budget_turns=bad)
    assert out["ok"] is False, "accepted %r" % (bad,)
    assert "budget_turns" in out["detail"]
    assert not CG._CONTRACT_FILE.exists(), (
        "a refused activation still wrote a contract file; the refusal has to leave no contract")


def test_an_unknown_op_class_is_still_refused_with_a_budget_present():
    """The pre-existing guard must not have been loosened by threading a new field through it."""
    out = CG.activate_contract(stop_when=("delet",), budget_turns=3)
    assert out["ok"] is False
    assert "unknown op_class" in out["detail"]
    assert not CG._CONTRACT_FILE.exists()


def test_the_budget_survives_a_read_in_another_reader():
    """contract_state/load_contract are what every consumer actually calls; the field has to be
    there for them, not only in the raw file."""
    CG.activate_contract(stop_when=("delete",), budget_turns=5)
    state, data = CG.contract_state()
    assert state == "active"
    assert data["budget_turns"] == 5
    assert CG.load_contract()["budget_turns"] == 5
