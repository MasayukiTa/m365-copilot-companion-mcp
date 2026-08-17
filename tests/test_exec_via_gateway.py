"""Execution tools are reachable through the gateway and not registered directly.

WHY THE REGISTRATION MATTERS AT ALL. It is not a tidiness question. The unlock gate keys on an
identity derived from a request header, and closing that properly needs a second factor
presented per call. The client cannot vary headers per call, so the only channel is a tool
argument -- and adding one to every gated tool is a change nobody can review. A single entry
point is what makes one reviewable change possible, so the execution family has to have one.

WHAT MUST NOT CHANGE is reachability. `call_tool` is registered first and the server's own
instructions already tell an agent that every tool lives behind it. These tests pin both
halves: the direct set no longer carries execution, and the gateway still invokes it.
"""
import importlib
import os

import pytest


EXEC = {"run_python", "shell_exec", "pwsh_exec", "pwsh_exec_file", "shell_which",
        "run_in_background", "run_python_in_background", "job_kill"}


def _registered(monkeypatch, **env):
    """Names the server would register directly, under the given environment."""
    for key in ("MCP_TOOL_MAP", "MCP_TOOL_MAP_MAX", "MCP_TOOL_MAP_EXEC_DIRECT",
                "MCP_TOOL_MAP_INCLUDE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("MCP_API_KEY", "test-key-not-a-real-one")
    import main
    importlib.reload(main)
    return {getattr(t, "__name__", "") for t in main.TOOLS}


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    yield
    # Leave the module in whatever state the rest of the suite expects.
    monkeypatch.delenv("MCP_TOOL_MAP", raising=False)


def test_execution_tools_are_not_registered_directly(monkeypatch):
    names = _registered(monkeypatch, MCP_TOOL_MAP="1")
    still_direct = EXEC & names
    assert not still_direct, "reachable without the gateway: %s" % sorted(still_direct)


def test_the_gateway_and_unlock_are_still_first(monkeypatch):
    """If call_tool were truncated away, removing the direct tools WOULD strand them."""
    names = _registered(monkeypatch, MCP_TOOL_MAP="1")
    assert "call_tool" in names
    assert "unlock" in names


def test_the_gateway_can_still_invoke_them(monkeypatch):
    """Reachability is the property that must not change. Checked, not assumed."""
    _registered(monkeypatch, MCP_TOOL_MAP="1")
    import main
    assert EXEC <= set(main._ALL_TOOLS), "the gateway's catalogue lost an execution tool"


def test_the_schema_budget_is_unchanged(monkeypatch):
    """The cap is about the agent's token budget, so the count matters as much as the names."""
    with_map = _registered(monkeypatch, MCP_TOOL_MAP="1", MCP_TOOL_MAP_MAX="70")
    direct = _registered(monkeypatch, MCP_TOOL_MAP="1", MCP_TOOL_MAP_MAX="70",
                         MCP_TOOL_MAP_EXEC_DIRECT="1")
    assert len(with_map) == len(direct), "the registered count moved"


def test_the_operator_can_put_them_back(monkeypatch):
    """A claim that usage is unchanged should be falsifiable by the operator, not only by me.

    Checked at a cap large enough for the question to be about the exclusion rather than
    about truncation -- this deployment runs MCP_TOOL_MAP_MAX=8, where the direct set is the
    priority tools and nothing else reaches it either way.
    """
    names = _registered(monkeypatch, MCP_TOOL_MAP="1", MCP_TOOL_MAP_MAX="70",
                        MCP_TOOL_MAP_EXEC_DIRECT="1")
    assert EXEC <= names


def test_this_deployment_already_registered_none_of_them(monkeypatch):
    """THE MEASUREMENT THAT CORRECTED ME.

    I reported that the gateway was "not a chokepoint" because the execution tools were
    registered directly. That was computed from the DEFAULT cap of 70 without reading the
    configuration: this deployment sets MCP_TOOL_MAP_MAX=8, so the direct set is unlock,
    call_tool and the six job-protocol tools, and the execution family has only ever been
    reachable through the gateway here.

    So the change above is a guarantee that does not depend on a cap staying small, not a
    removal of something that was in use. Recorded as a test because the earlier claim is in
    a commit message and a reader deserves to find the correction next to the code.
    """
    names = _registered(monkeypatch, MCP_TOOL_MAP="1", MCP_TOOL_MAP_MAX="8")
    assert not (EXEC & names)
    assert "call_tool" in names and "unlock" in names
    assert len(names) == 8


def test_a_large_cap_is_where_the_exclusion_actually_does_work(monkeypatch):
    """At cap=70 the old code DID register them directly. That is the case being fixed."""
    kept = _registered(monkeypatch, MCP_TOOL_MAP="1", MCP_TOOL_MAP_MAX="70",
                       MCP_TOOL_MAP_EXEC_DIRECT="1")
    removed = _registered(monkeypatch, MCP_TOOL_MAP="1", MCP_TOOL_MAP_MAX="70")
    assert EXEC <= kept
    assert not (EXEC & removed)
