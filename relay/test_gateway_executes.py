"""Call the gateway. Do not read it.

Every other test of the routing work asserts on main.py's SOURCE, and all of them passed while
the gateway raised UnboundLocalError on every call it served: the routing block read `_args`
sixty-five lines before that name is bound. Nothing routed, nothing ran locally, and the
fleet's workers reported STUCK for twenty minutes -- which reads exactly like models failing at
the task rather than a gateway that cannot execute.

A source assertion cannot catch a name that is not bound yet. These tests import the module and
call the function.
"""
import os

import pytest


@pytest.fixture(scope="module")
def gateway():
    """main.py's call_tool, with the tool map enabled (it is defined only under that flag)."""
    os.environ["MCP_TOOL_MAP"] = "1"
    # main.py reads its configuration at import. These are placeholders: nothing here talks to
    # a network, and the point is to execute the gateway function, not to serve requests.
    os.environ.setdefault("MCP_API_KEY", "test-key-not-used")
    os.environ.setdefault("MCP_ALLOWED_BASE", os.getcwd())
    import importlib
    import main as M
    importlib.reload(M)
    assert hasattr(M, "call_tool"), "call_tool is defined only when MCP_TOOL_MAP=1"
    return M.call_tool


def test_the_catalogue_call_returns_rather_than_raising(gateway):
    out = gateway(name="")
    assert isinstance(out, str) and "tools available" in out


def test_a_help_call_returns_rather_than_raising(gateway):
    out = gateway(name="list_directory")
    assert isinstance(out, str)


def test_an_actual_tool_call_reaches_the_tool(gateway):
    """THE ONE THAT WOULD HAVE CAUGHT IT. Everything above the argument parsing is reachable
    with `arguments=None`; only a real call binds `_args` and runs the routing block.

    The path is inside the server's allowed base -- a tmp_path is refused by the path guard
    before the tool runs, which would make this pass for the wrong reason.
    """
    target = os.path.join(os.getcwd(), "relay")
    out = gateway(name="list_directory", arguments={"path": target})
    assert isinstance(out, str)
    assert "UnboundLocalError" not in out
    assert "outside the allowed base" not in out
    # A file that is actually in relay/. This named fleet_tool_router.py, which moved
    # to bench/remote/ when routing left the shipped server -- the test then failed on
    # a relocation rather than on the property it exists to check.
    assert "relay_fleet.py" in out


def test_an_unknown_tool_is_reported_not_raised(gateway):
    out = gateway(name="definitely_not_a_tool", arguments={})
    assert "unknown tool" in out
