# -*- coding: utf-8 -*-
"""The autonomy gate could be turned OFF and never ON, so it was inert unless someone hand-wrote JSON.

`activate_contract()` says so in its own docstring:

    Write .fleet/active_contract.json with active=true. The missing half of
    deactivate_contract() (codex-plan item 3, 2026-09-09): nothing in this codebase could
    turn a contract ON before this, only off.

It was written to close that gap and then had no caller, so the gap stayed open and it sat in
this repository's unreached baseline. Measured 2026-09-13: `deactivate_contract()` is called at
relay/fleet_runner.py:2523 (run end); `activate_contract` is called nowhere.

THE CONSUMING SIDE WAS ALREADY WIRED, which is what made this worth fixing rather than
deleting. `run_relay_fleet` reads the contract and caps every worker by it:

    _c = load_contract()
    if _c is not None and _c.get("active") and isinstance(_c.get("budget_turns"), int) ...
        _contract_budget = _c["budget_turns"]
    effective_max_turns = min(max_turns, _contract_budget) ...

and a worker that reaches it stops with "autonomy contract: turn budget %d reached". Every part
of the mechanism existed except the ability to start it.

WHAT IS DELIBERATELY NOT DECIDED. Nothing in the code says WHEN a contract should become
active or WHAT it should gate; the module docstring names the cockpit only as the party that
ANSWERS a pending gate. Activating one automatically would be choosing the policy -- which op
classes to gate, what budget -- and a gate whose contents were guessed is worse than none: it
reads as protection somebody chose. So the CLI is an operator surface and carries no policy,
and the intended automatic caller, if one is meant to exist, is NOT IDENTIFIED.

EVERY TEST HERE REDIRECTS `_FLEET_DIR` INTO tmp_path. Running `activate` against the real
directory would arm a live gate on the operator's machine -- which happened once already today,
through this same module, when a demo wrote a real gate file into the live directory.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import tools.contract_gate as CG  # noqa: E402


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point every file this module writes at tmp_path, and prove it took."""
    monkeypatch.setattr(CG, "_FLEET_DIR", Path(tmp_path))
    monkeypatch.setattr(CG, "_CONTRACT_FILE", Path(tmp_path) / "active_contract.json")
    assert not (Path(REPO) / ".fleet" / "active_contract.json").samefile(
        CG._CONTRACT_FILE) if CG._CONTRACT_FILE.exists() else True
    return Path(tmp_path)


def _state(capsys):
    CG.main(["show"])
    return capsys.readouterr().out


# ── the gap ───────────────────────────────────────────────────────────────────────────────

def test_a_contract_can_be_turned_on(isolated, capsys):
    """THE DEFECT. Before this there was no way to reach active_contract() at all."""
    rc = CG.main(["activate", "--ask-before", "delete", "--budget-turns", "12"])
    assert rc == 0, capsys.readouterr().out
    state, data = CG.contract_state()
    assert state == "active"
    assert data["ask_before"] == ["delete"]
    assert data["budget_turns"] == 12


def test_the_fleet_would_see_it(isolated):
    """The consuming side reads through load_contract(); an activation the fleet cannot see
    would be a control that reports success and gates nothing."""
    CG.main(["activate", "--ask-before", "delete", "--budget-turns", "7"])
    c = CG.load_contract()
    assert c is not None and c.get("active") is True
    assert c.get("budget_turns") == 7


def test_it_can_be_retired_again(isolated):
    CG.main(["activate", "--ask-before", "delete"])
    assert CG.contract_state()[0] == "active"
    assert CG.main(["deactivate"]) == 0
    assert CG.contract_state()[0] != "active"


# ── the refusals the function already had, reachable now ──────────────────────────────────

def test_a_second_activation_is_refused_not_silently_clobbered(isolated, capsys):
    """activate_contract refuses rather than overwriting, because the second one's identity
    would silently replace the first's and whoever relies on the first for their retirement
    record would retire a contract that is no longer the one enforcing anything."""
    assert CG.main(["activate", "--ask-before", "delete"]) == 0
    rc = CG.main(["activate", "--ask-before", "outbound"])
    out = capsys.readouterr().out
    assert rc != 0, out
    assert "refused" in out
    assert CG.contract_state()[1]["ask_before"] == ["delete"], "先の契約が上書きされた"


def test_an_unknown_op_class_is_refused(isolated, capsys):
    """An op_class this file does not recognise gates nothing, silently -- 'a contract that
    silently gates nothing is worse than no contract; it reads as protection that was never
    there'."""
    rc = CG.main(["activate", "--ask-before", "delete,teleport"])
    out = capsys.readouterr().out
    assert rc != 0, out
    assert CG.contract_state()[0] != "active"


# ── the surface itself ────────────────────────────────────────────────────────────────────

def test_show_says_plainly_when_nothing_is_gated(isolated, capsys):
    out = _state(capsys)
    assert "absent" in out
    assert "nothing is gated" in out


def test_show_does_not_change_anything(isolated):
    CG.main(["show"])
    assert CG.contract_state()[0] == "absent"


def test_no_argument_is_show_not_activate(isolated):
    """A CLI whose default action ARMS a gate is a trap. The empty invocation reports."""
    CG.main([])
    assert CG.contract_state()[0] == "absent"
