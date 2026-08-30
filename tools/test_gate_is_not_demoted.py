"""The demotion of the dangerous-command regexes was REMOVED, and this records why.

The plan called for turning these regexes into an audit signal once fleet execution moved into
a container, on the argument that holding a command for human approval when it cannot run here
turns an approval queue into noise people click through. That argument is right -- and it needs
no code, because it is already true structurally: a call that is routed returns from the
gateway before local dispatch and never reaches check_op.

What the shipped demotion checked was whether ROUTING WAS SWITCHED ON, which is a different
question from whether THIS call was routed. With the switch on, an operator's call -- or any
call naming a path no container owns -- passes through and runs here. So the demotion went
quiet for exactly the commands that were still about to execute on this machine.
"""
import io
import os
import re

GATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "contract_gate.py")


def _code():
    """Source with comments and docstrings removed.

    The file now EXPLAINS the removed demotion at length, so a raw-source search for
    "broker_client" matches the explanation and reports the code as still present. Stripping
    first is the difference between asserting on the rule and asserting on the prose about it.
    """
    import ast
    src = io.open(GATE, encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            d = ast.get_docstring(node)
            if d:
                src = src.replace(d, "")
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in src.splitlines())


def test_the_gate_does_not_consult_the_routing_switch():
    code = _code()
    assert "broker_client" not in code, (
        "the gate reads whether routing is switched on; that is not whether this call was "
        "routed, and every call that reaches check_op is about to run locally")


def test_the_gate_does_not_write_an_audit_record_instead_of_gating():
    code = _code()
    assert "gate_audit" not in code, (
        "recording the match instead of gating it leaves the command free to run here")
