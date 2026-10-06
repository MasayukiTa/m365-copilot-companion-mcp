"""Call the gateway. Do not read it.

Every other test of the routing work asserts on main.py's SOURCE, and all of them passed while
the gateway raised UnboundLocalError on every call it served: the routing block read `_args`
sixty-five lines before that name is bound. Nothing routed, nothing ran locally, and the
fleet's workers reported STUCK for twenty minutes -- which reads exactly like models failing at
the task rather than a gateway that cannot execute.

A source assertion cannot catch a name that is not bound yet. These tests import the module and
call the function.
"""
import importlib
import os

import pytest


@pytest.fixture(scope="module")
def gateway():
    """main.py's call_tool, with the tool map enabled (it is defined only under that flag)."""
    had_tool_map = "MCP_TOOL_MAP" in os.environ
    prev_tool_map = os.environ.get("MCP_TOOL_MAP")
    had_api_key = "MCP_API_KEY" in os.environ
    had_allowed_base = "MCP_ALLOWED_BASE" in os.environ
    os.environ["MCP_TOOL_MAP"] = "1"
    # main.py reads its configuration at import. These are placeholders: nothing here talks to
    # a network, and the point is to execute the gateway function, not to serve requests.
    os.environ.setdefault("MCP_API_KEY", "test-key-not-used")
    os.environ.setdefault("MCP_ALLOWED_BASE", os.getcwd())
    import main as M
    importlib.reload(M)
    assert hasattr(M, "call_tool"), "call_tool is defined only when MCP_TOOL_MAP=1"
    yield M.call_tool

    # Teardown: importlib.reload(M) above rebound EVERY module-level name on `main` --
    # API_KEY, TOOLS, _ALL_TOOLS -- to whatever this fixture's synthetic env said. Nothing
    # else in this file reloads main again, so without this, the synthetic MCP_API_KEY (and
    # a forced MCP_TOOL_MAP=1) leak into every later test file in the same pytest session --
    # the exact failure 73e3624 fixed for tests/test_exec_via_gateway.py and
    # tests/test_tool_map_pinning.py (main.API_KEY leaking into
    # tests/test_health_route.py's bearer-token checks). This fixture has no dependency on
    # `monkeypatch`, so pytest tears it down after every function-scoped fixture in this
    # module -- restore the environment first, then reload once more so main's module-level
    # names come back from the REAL environment.
    if had_tool_map:
        os.environ["MCP_TOOL_MAP"] = prev_tool_map
    else:
        os.environ.pop("MCP_TOOL_MAP", None)
    if not had_api_key:
        os.environ.pop("MCP_API_KEY", None)
    if not had_allowed_base:
        os.environ.pop("MCP_ALLOWED_BASE", None)
    importlib.reload(M)


def test_the_catalogue_call_returns_rather_than_raising(gateway):
    """The phrase changed; the property did not.

    This asserted the literal "tools available", which was the flat catalogue's opening line.
    On 2026-09-15 that list became an INDEX of categories -- 16,594 characters to 4,445 --
    and the phrase went with it, so the test failed on wording rather than on the property it
    is named for. Asserting the new opening line would only re-arm the same trap, so what is
    checked here is what a caller actually needs back: a non-trivial string that says how to
    open a category and how to run a tool.
    """
    out = gateway(name="")
    assert isinstance(out, str) and len(out) > 200
    assert "call_tool(name=" in out, out[:200]
    from tools import tool_catalogue as tc
    import main as M
    for key in tc.by_category(M._ALL_TOOLS):
        assert key in out, "the index does not name category %r" % key


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
    """Same stale-wording fix, and the behaviour behind it got better rather than only different.

    The message was "unknown tool '<name>'. Use call_tool(name='') to list all." -- measured
    76 times in six hours of the ledger, each one a wasted round trip that ended in a
    re-listing. It now names the closest tools and the categories, so the miss is answered
    instead of merely reported. What this test holds is the original property: a name that
    does not exist comes back as text, never as an exception, and the text identifies what
    was asked for.
    """
    out = gateway(name="definitely_not_a_tool", arguments={})
    assert isinstance(out, str)
    assert "definitely_not_a_tool" in out
    assert "call_tool(name='')" in out, out[:200]
