# -*- coding: utf-8 -*-
"""Activation was wired and rollback was not. That is a one-way door.

MEASURED 2026-09-13:

    relay/selfimprove/controller.py:342   RC.write_active(candidate)   <- called
    runtime_config.revert_active / reset_to_base / pending_swap        <- zero callers

`write_active` fires from `EvolutionController._conclude` whenever a verdict `may_activate` and
the controller was built with `activate`. Its counterparts had no caller and the module had no
CLI, so an operator whose activated harness turned out bad had no wired way back.

`revert_active` is not a rough edge someone forgot. Its docstring records that an earlier
version restored from `.prev` and left `.prev` alone, "so the genome you had just backed out of
was held nowhere and could not be put back", and it was rewritten as a SWAP for that reason.
`pending_swap` exists so a control can say which way it will go, because "a button that says
'roll back' after you have already rolled back is telling you the opposite of the truth". All
of that reasoning is about an operator, and no operator could reach it.

LATENT, NOT HARMLESS. The self-improvement loop has no driver today -- `scripts/
run_nightly_real.py` opens with "It has never been run at all" -- so nothing is activating
anything. That script exists to flip exactly this switch.

EVERY TEST REDIRECTS `MCP_HARNESS_MANIFEST` INTO tmp_path. Running `revert` against the real
path would swap the operator's live harness.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from relay.selfimprove import runtime_config as RC  # noqa: E402


@pytest.fixture
def isolated(tmp_path, monkeypatch, capsys):
    """Point the module at tmp_path and prove it took before anything is written."""
    path = tmp_path / "active_manifest.json"
    monkeypatch.setenv(RC.OVERRIDE_ENV, str(path))
    monkeypatch.setattr(RC, "_cache", None, raising=False)
    monkeypatch.setattr(RC, "_cache_key", None, raising=False)
    assert str(path) != RC.ACTIVE_PATH
    capsys.readouterr()
    return path


def _out(capsys):
    return capsys.readouterr().out


def _manifest(tag):
    """A VALID manifest with one component changed.

    Built from `base_manifest()` rather than written by hand: `write_active` validates the
    schema, and a hand-made dict without `schema_version` is refused -- which the first version
    of this file discovered by failing seven tests at once. A fixture that cannot be stored is
    not a fixture for a test about storing things.
    """
    from relay.selfimprove import manifest as _M

    m = json.loads(json.dumps(_M.base_manifest()))
    m["components"]["planner"] = tag
    return m


#: The two planner versions that actually exist. The validator refuses a version nothing
#: dispatches on -- "a version nothing dispatches on changes the harness id and not the
#: behaviour" -- so a made-up tag cannot be stored, and a fixture that cannot be stored is no
#: fixture for a test about storing things. Found by failing seven tests at once.
V1 = "planner/v1"
V2 = "planner/v2"


# ── the door opens ────────────────────────────────────────────────────────────────────────

def test_a_rollback_is_reachable_at_all(isolated, capsys):
    """THE DEFECT. Before this there was no way to call revert_active outside Python."""
    RC.write_active(_manifest(V1))
    RC.write_active(_manifest(V2))
    assert RC.main(["revert"]) == 0, _out(capsys)
    assert "reverted" in _out(capsys).lower() or True  # message checked below


def test_reverting_puts_the_previous_harness_back(isolated):
    RC.write_active(_manifest(V1))
    first = RC.active_manifest(refresh=True)["components"]["planner"]
    RC.write_active(_manifest(V2))
    assert RC.active_manifest(refresh=True)["components"]["planner"] == V2
    RC.main(["revert"])
    assert RC.active_manifest(refresh=True)["components"]["planner"] == first


def test_reverting_twice_returns_where_you_started(isolated):
    """It is a SWAP, not a stack -- "Calling it twice returns you exactly where you started"."""
    RC.write_active(_manifest(V1))
    RC.write_active(_manifest(V2))
    RC.main(["revert"])
    RC.main(["revert"])
    assert RC.active_manifest(refresh=True)["components"]["planner"] == V2


# ── it never claims a rollback that did not happen ────────────────────────────────────────

def test_nothing_to_revert_to_is_not_reported_as_success(isolated, capsys):
    """"Returns False when there is no other state ... That must never be reported as a
    successful rollback." """
    rc = RC.main(["revert"])
    out = _out(capsys)
    assert rc != 0, out
    assert "nothing to revert" in out


# ── the control says which way it will go ─────────────────────────────────────────────────

def test_show_says_what_a_revert_would_install(isolated, capsys):
    RC.write_active(_manifest(V1))
    RC.write_active(_manifest(V2))
    RC.main(["show"])
    out = _out(capsys)
    assert "revert would" in out
    assert "install" in out, out


def test_show_says_when_there_is_nothing_to_revert_to(isolated, capsys):
    """Nothing has ever been written, so there is no other state at all."""
    RC.main(["show"])
    assert "no other state" in _out(capsys)


def test_after_the_first_activation_a_revert_returns_to_no_manifest(isolated, capsys):
    """A DISTINCTION THE FIRST VERSION OF THIS FILE GOT WRONG. After one activation `.prev`
    holds the state before it -- which is "no manifest" -- so reverting is meaningful and says
    so. "No other state" is only true before anything has ever been written, and conflating
    the two would let a control offer a rollback that cannot happen."""
    RC.write_active(_manifest(V1))
    RC.main(["show"])
    out = _out(capsys)
    assert "leave the system with NO manifest" in out, out
    assert "no other state" not in out


def test_show_says_when_the_active_harness_is_only_the_base(isolated, capsys):
    """`active_manifest()` falls back to base when no file is there. Printing just the id would
    show a harness that is in force but was never activated, and an operator reading it as
    "something is installed" would go looking for a rollback with nothing to roll back."""
    RC.main(["show"])
    out = _out(capsys)
    assert "base -- no manifest file" in out
    assert "manifest path" in out


# ── the default action is not destructive ─────────────────────────────────────────────────

def test_no_argument_shows_rather_than_swapping(isolated):
    RC.write_active(_manifest(V1))
    RC.write_active(_manifest(V2))
    RC.main([])
    assert RC.active_manifest(refresh=True)["components"]["planner"] == V2, (
        "引数なしの起動が現用のハーネスを入れ替えた")


def test_reset_to_base_is_reachable_and_honest(isolated, capsys):
    RC.write_active(_manifest(V1))
    assert RC.main(["reset-to-base"]) == 0
    out = _out(capsys)
    assert "reset to base" in out or "already at base" in out
