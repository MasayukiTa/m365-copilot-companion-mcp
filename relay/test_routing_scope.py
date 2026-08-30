"""Routing must contain the FLEET, without refusing the operator -- and the predicate that
separates them has to be one that is actually true during a run.

Two wrong predicates were shipped before this file said so:

  1. `broker_client.enabled()` alone, read on every call the gateway serves. An operator's
     call names a path no container owns, and routing-on turns "cannot be placed" into a
     refusal, so switching routing on would have refused ordinary work.
  2. `_fleet_run_active()`, which reads .fleet/active_contract.json. That file is written by
     an operator arming an autonomy contract, NOT by a bench run, so during the first real
     routed run it read False: every worker executed on this machine, in the address
     directories staging had just stopped filling. The switch was on and nothing was
     contained.

What actually separates the populations is whether the path belongs to a staged instance.
"""
import io
import os
import re

import pytest

from relay import fleet_tool_router as R

MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")


def _code():
    """Source with comments and docstrings stripped.

    Asserting against raw source matches the prose explaining a rule as readily as the rule,
    so a file that only TALKS about a guard passes. That has produced both false greens and
    false reds in this repository before.
    """
    import ast
    src = io.open(MAIN, encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            d = ast.get_docstring(node)
            if d:
                src = src.replace(d, "")
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in src.splitlines())


def test_a_call_outside_the_staging_root_is_not_the_fleets(tmp_path):
    assert R.is_fleet_path(str(tmp_path)) is False


def test_a_call_under_the_staging_root_is_the_fleets():
    assert R.is_fleet_path(os.path.join(R.STAGING_ROOT, "p99", "src")) is True
    assert R.is_fleet_path(R.STAGING_ROOT) is True


def test_an_unstaged_fleet_path_is_refused_not_passed_through(monkeypatch):
    """Under the staging root but owned by no instance: that is a placement failure, and
    running it here would leave the run looking identical and unconfined."""
    monkeypatch.setattr(R, "_worktrees", lambda: {})
    with pytest.raises(R.NotRoutable) as exc:
        R.route("shell_exec", {"working_dir": os.path.join(R.STAGING_ROOT, "p99"),
                               "command": "echo hi"})
    assert not isinstance(exc.value, R.NotAFleetPath)


def test_an_ordinary_path_raises_the_pass_through_signal(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "_worktrees", lambda: {})
    with pytest.raises(R.NotAFleetPath):
        R.route("shell_exec", {"working_dir": str(tmp_path), "command": "echo hi"})


def test_the_gateway_passes_through_only_the_not_ours_signal():
    code = _code()
    assert "except _router.NotAFleetPath:" in code, (
        "the gateway must distinguish 'not the fleet's call' from 'could not be placed'")
    # NotAFleetPath subclasses NotRoutable, so the pass-through handler must come FIRST or it
    # never runs and every operator call is refused.
    assert code.index("except _router.NotAFleetPath:") < code.index("except _router.NotRoutable"), (
        "NotAFleetPath subclasses NotRoutable; ordered the other way the refusal catches "
        "everything and the operator is locked out")


def test_the_gateway_does_not_gate_on_the_autonomy_contract():
    code = _code()
    assert "_fleet_run_active" not in code, (
        "the autonomy contract is a different mechanism, written by an operator rather than "
        "by a run; gating routing on it made routing read False during the run it was meant "
        "to contain")
