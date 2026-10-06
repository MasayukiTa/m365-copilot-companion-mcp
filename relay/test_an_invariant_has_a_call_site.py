# -*- coding: utf-8 -*-
"""A post-condition that is checked while the code runs, instead of when somebody looks.

WHAT THIS IS A PORT OF. An external harness (DeepSeek's `dsh`) has each package register its
own invariants and raise at runtime with a machine-readable code and the violated package's
name. Its analysis of this repository named the contrast exactly: a baseline turns a broken
state into a KNOWN state and leaves it green, and an invariant is the opposite -- verified every
execution, loud the moment it stops holding. Its own porting note asked for the minimum:
"a thin layer that asserts post-conditions on important paths and raises a machine-readable
code -- assert_invariant(name, cond, msg) is enough".

WHY TWO DISPOSITIONS. dsh raises on every violation. Here a raise on the fleet's hot path costs
a fifty-minute run, and "telemetry must not be able to fail a run" is a rule this repository
already paid for. So RAISE is for the cases where a wrong answer is worse than a stop -- the
reconciler reading 2 of 1557 transcripts and reporting on them -- and RECORD is for recovery
paths, where the violation is written down and the caller carries on. Both end the silence,
which is the part that was missing.

THE FAILURE MODE THIS FILE EXISTS AGAINST. The source's own note: "if nobody writes the
invariants, nothing is protected." A registry of promises nothing checks is precisely the
defect the unreached burn-down has been chasing all week, one level up. So every registered
name must appear at a `assert_invariant(` call site in non-test code, and that is checked here.
"""
from __future__ import annotations

import ast
import io
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from relay import invariants as INV  # noqa: E402

# AT MODULE SCOPE, DELIBERATELY. These two modules call `invariants.register()` at import, and
# that is the thing under test -- a promise is declared when the module loads, not when someone
# asks. Importing them inside `_registered_names()` meant the FIRST test to call it performed
# the import, so the registration landed in that test's throwaway copy of the registry (see
# `_clean_registry`) and was restored away before the next test looked. The file then passed
# only when some other file in the run had already imported them, which is exactly what
# happened: alone it failed, and with relay/test_fleet_reconcile.py in front of it, it passed.
import relay.fleet_reconcile  # noqa: E402,F401
import relay.relay_fleet      # noqa: E402,F401


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch, tmp_path):
    """Each test gets its own registry and ledger: a registration leaking between tests would
    make the call-site check below pass on a name the repository does not actually declare."""
    monkeypatch.setattr(INV, "REGISTRY", dict(INV.REGISTRY))
    monkeypatch.setattr(INV, "LOG", str(tmp_path / "invariants.jsonl"))


# ── the layer ─────────────────────────────────────────────────────────────────────────────

def test_a_holding_invariant_says_nothing():
    INV.register("t.ok", "t", INV.RAISE, "because")
    assert INV.assert_invariant("t.ok", True, "never printed") is True
    assert INV.violations(INV.LOG) == []


def test_a_raise_invariant_stops_the_caller():
    INV.register("t.raise", "t", INV.RAISE, "a wrong answer is worse than a stop")
    with pytest.raises(INV.InvariantViolated) as e:
        INV.assert_invariant("t.raise", False, "it broke", n=3)
    assert e.value.code == "INVARIANT"
    assert e.value.owner == "t"
    assert "t.raise" in str(e.value) and "it broke" in str(e.value)


def test_a_record_invariant_lets_the_caller_continue(capsys):
    """The recovery-path disposition. Raising here would trade a wasted attempt for a lost
    run, which is the opposite of what the path is for."""
    INV.register("t.record", "t", INV.RECORD, "stopping is the worse outcome here")
    assert INV.assert_invariant("t.record", False, "it broke") is False
    assert "[INVARIANT]" in capsys.readouterr().out


def test_a_violation_is_on_disk_even_if_nothing_catches_it():
    """The row is written BEFORE the raise. An exception swallowed by an `except Exception`
    three frames up is how the last one of these stayed invisible."""
    INV.register("t.raise2", "t", INV.RAISE, "because")
    with pytest.raises(INV.InvariantViolated):
        INV.assert_invariant("t.raise2", False, "it broke", where="here")
    rows = INV.violations(INV.LOG)
    assert len(rows) == 1
    assert rows[0]["code"] == "INVARIANT"
    assert rows[0]["invariant"] == "t.raise2"
    assert rows[0]["context"] == {"where": "here"}


def test_it_is_an_assertion_error():
    """So an `except Exception` written years before this layer existed still catches it. An
    invariant that can kill a run through an old handler is worse than the silence."""
    assert issubclass(INV.InvariantViolated, AssertionError)


def test_an_unregistered_name_is_a_mistake_not_a_pass():
    """A typo in a name must not silently become an invariant that never fires."""
    with pytest.raises(KeyError):
        INV.assert_invariant("t.never-declared", False, "it broke")


def test_a_registration_with_no_reason_is_refused():
    with pytest.raises(ValueError):
        INV.register("t.bare", "t", INV.RAISE, "   ")


def test_an_unknown_disposition_is_refused():
    with pytest.raises(ValueError):
        INV.register("t.weird", "t", "warn", "because")


def test_it_can_be_switched_off_by_name(monkeypatch):
    INV.register("t.off", "t", INV.RAISE, "because")
    monkeypatch.setenv("MCP_INVARIANTS_OFF", r"^t\.off$")
    assert INV.assert_invariant("t.off", False, "it broke") is True
    monkeypatch.delenv("MCP_INVARIANTS_OFF")
    with pytest.raises(INV.InvariantViolated):
        INV.assert_invariant("t.off", False, "it broke")


def test_an_allowlist_narrows_to_what_it_names(monkeypatch):
    INV.register("t.in", "t", INV.RAISE, "because")
    INV.register("t.out", "t", INV.RAISE, "because")
    monkeypatch.setenv("MCP_INVARIANTS", r"^t\.in$")
    with pytest.raises(INV.InvariantViolated):
        INV.assert_invariant("t.in", False, "it broke")
    assert INV.assert_invariant("t.out", False, "it broke") is True


# ── the promises this repository actually makes ───────────────────────────────────────────

def _registered_names():
    """Whatever the production modules declared at import. They are imported at the top of this
    file, not here -- see the note beside those imports for the bug that caused."""
    return dict(INV.REGISTRY)


def _assert_call_names():
    """Every string literal handed to an `assert_invariant(` call, plus every constant that holds one."""
    names, consts = set(), {}
    for pkg in ("relay", "tools", "bench", "bridge", "scripts"):
        root = os.path.join(REPO, pkg)
        if not os.path.isdir(root):
            continue
        for dirpath, _d, files in os.walk(root):
            if "__pycache__" in dirpath:
                continue
            for fn in files:
                if not fn.endswith(".py") or fn.startswith("test_"):
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    tree = ast.parse(io.open(path, encoding="utf-8", errors="replace").read())
                except Exception:
                    continue
                for node in ast.walk(tree):
                    # `_INV_X = _inv.register("name", ...)` -- the handle a call site uses.
                    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                        f = node.value.func
                        if getattr(f, "attr", None) == "register" and node.value.args:
                            a0 = node.value.args[0]
                            if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                                for t in node.targets:
                                    if isinstance(t, ast.Name):
                                        consts[t.id] = a0.value
                    if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "assert_invariant":
                        if not node.args:
                            continue
                        a0 = node.args[0]
                        if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                            names.add(a0.value)
                        elif isinstance(a0, ast.Name):
                            names.add(a0.id)
    return {consts.get(n, n) for n in names}


def test_every_registered_invariant_is_checked_somewhere():
    """THE POINT. A registry of promises nothing verifies is the same defect as a function
    nobody calls -- which is what the whole burn-down this came out of is about."""
    registered = set(_registered_names())
    checked = _assert_call_names()
    assert registered, "何も登録されていない: レジストリを読めていない"
    unchecked = sorted(registered - checked)
    assert not unchecked, (
        "登録されているのに assert_invariant() されていない: %s" % ", ".join(unchecked))


def test_every_invariant_states_why_it_exists():
    thin = sorted(n for n, e in _registered_names().items()
                  if len((e.get("why") or "").strip()) < 40)
    assert not thin, "理由が書かれていない: %s" % ", ".join(thin)


def test_the_registry_is_populated_before_any_fixture_copies_it():
    """THE BUG THIS FILE HAD. `_clean_registry` hands each test its own copy of the registry, so
    a registration that happens DURING a test is discarded when monkeypatch restores. Production
    invariants must therefore be declared at import -- which is what the layer promises anyway:
    "declared up front so the set of promises this repository makes at runtime can be listed".

    Asserted against the copy this test was given, not against a fresh import, because the copy
    is what every other test in the file reads."""
    assert "reconciler.transcripts_are_readable" in INV.REGISTRY, (
        "the registry this test was handed is missing a production invariant, which means the "
        "registering module was imported lazily and its declaration was thrown away with some "
        "earlier test's copy")


def test_the_reconciler_promise_is_the_one_that_failed():
    """Pinned by name because it is the measurement the layer was built from: 2 goals out of
    1557 transcripts, reported as though the reader had looked."""
    reg = _registered_names()
    assert reg["reconciler.transcripts_are_readable"]["disposition"] == INV.RAISE
    assert reg["socket_route.reset_keeps_no_token"]["disposition"] == INV.RECORD
