"""Routing must contain the FLEET, not the operator.

`main.call_tool` consulted `broker_client.enabled()` on every call it served. Switching
routing on to move fleet worktrees off this machine would therefore have refused the
operator's own calls as well -- every one of them names a path no container owns, so every
one hits NotRoutable, which routing-on turns into a refusal. A containment measure that has
to be switched off to get ordinary work done is one that will be found switched off.
"""
import io
import os
import re

MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")


def _code():
    """Source with comments and docstrings removed.

    Asserting against raw source matches the prose that explains the rule as readily as the
    rule, so a file that only TALKS about a guard passes. That has produced both false greens
    and false reds here before.
    """
    src = io.open(MAIN, encoding="utf-8").read()
    import ast
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            d = ast.get_docstring(node)
            if d:
                src = src.replace(d, "")
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in src.splitlines())


def test_routing_is_gated_on_an_active_fleet_run():
    code = _code()
    m = re.search(r"if\s+_bc\.enabled\(\)([^\n:]*):", code)
    assert m, "the routing switch is not read where it was"
    assert "_fleet_active()" in m.group(1), (
        "routing is read on every call, not only during a fleet run: %r" % m.group(1))


def test_the_predicate_comes_from_the_gate_that_fails_closed():
    code = _code()
    assert "from relay.fleet_toolset import _fleet_run_active" in code, (
        "the run predicate must be the one that reads contract_state, which treats a missing "
        "or corrupt policy file as tampering rather than as 'no run in flight'")


def test_an_unroutable_call_is_still_refused_not_run_here():
    code = _code()
    assert "NotRoutable" in code and "call_tool refused" in code, (
        "with routing on, a call that cannot be placed in a container must be refused; "
        "running it locally would leave the run looking identical and unconfined")
