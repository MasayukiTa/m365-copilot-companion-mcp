"""The retry trigger reads the worker's own report, which is where it fails.

DONE is in NON_RETRYABLE, so a worker that says it finished is never tried again -- and the
grader says such a claim is right 71.8% of the time. The population that most needs another
attempt is precisely the one the trigger skips. This is not inferred from the data; it is what
the outcome table says.
"""
import inspect

from relay import outcomes as O
from relay import relay_fleet as RF


def _code():
    """The runner's source with docstrings dropped, so an assertion cannot match its own
    explanation. That trap has been walked into twice today."""
    import ast
    tree = ast.parse(inspect.getsource(RF.run_relay_fleet))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body = node.body[1:]
    return ast.unparse(tree)


def test_done_is_not_retryable_which_is_the_whole_problem():
    """Stated as a test so a future change to the table is a deliberate one."""
    assert "DONE" in O.NON_RETRYABLE
    assert "DONE" not in O.RETRYABLE
    assert set(O.RETRYABLE) == {"STUCK", "INFRA_STUCK", "REFUSED"}


def test_the_decision_is_recorded_whether_or_not_it_fires():
    """A funnel that only records firings cannot show a mechanism declining to fire, which is
    the state this one is almost always in."""
    code = _code()
    assert 'mechanism_telemetry' in code
    assert 'not_triggered_reason' in code


def test_the_unverified_done_path_is_opt_in():
    """Off by default. The evidence is one rescue over five considered instances, and the cost
    is roughly a second attempt on every goal that claims to have finished -- the shape a
    measurement grows from, not a default to adopt at that n."""
    code = _code()
    assert "MCP_RETRY_UNVERIFIED_DONE" in code
    # It must require the flag AND an absent verification, not either alone.
    assert "verified" in code


def test_it_only_reaches_answers_nothing_checked():
    """`verified` is None when no gate ever ran. A DONE that a gate PASSED is not the false
    positive this targets, and retrying it would spend turns on answers already checked."""
    code = _code()
    i = code.index("MCP_RETRY_UNVERIFIED_DONE")
    window = code[max(0, i - 400):i + 200]
    assert "verified" in window and "None" in window
