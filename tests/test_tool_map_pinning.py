# -*- coding: utf-8 -*-
"""MCP_TOOL_MAP_INCLUDE named a tool, and the server dropped it without a word.

The operator's .env carried `MCP_TOOL_MAP_INCLUDE=list_directory` and had for as long as
anyone could tell. It never took effect. The setting is documented as pinning a tool "right
after the priority set", and the priority set alone filled the budget -- so a pin could never
be reached, and the truncation happened in silence, which is how a setting reads as working
for months.

Raising MCP_TOOL_MAP_MAX did not fix it either: the next slot went to list_unlocked, whose
schema is 367 tokens, rather than to the 168-token tool actually asked for.

Two things are pinned here. An operator naming a tool outranks a hardcoded default tail, and
whatever the cap discards, it says so.
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: The tools that must be present whatever else is: the unlock gate, the gateway everything
#: else lives behind, and the door an agent hands work through.
ESSENTIAL = {"unlock", "call_tool", "fleet_submit"}

#: The tail the priority list offers when there is room. None of these is load-bearing.
NICE = {"list_unlocked", "list_my_tools", "env_info"}


def _registered(monkeypatch, **env):
    for key in ("MCP_TOOL_MAP", "MCP_TOOL_MAP_MAX", "MCP_TOOL_MAP_EXEC_DIRECT",
                "MCP_TOOL_MAP_INCLUDE", "MCP_EXECUTION_PROFILES"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("MCP_API_KEY", "test-key-not-a-real-one")
    import main
    importlib.reload(main)
    return [getattr(t, "__name__", "") for t in main.TOOLS]


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    yield
    # Leave the module as the rest of the suite expects, WITHOUT reloading it here: a reload
    # in teardown runs after monkeypatch has already put MCP_API_KEY back, and main.py reads
    # that at import. The first version errored on every test for exactly that.
    monkeypatch.delenv("MCP_TOOL_MAP", raising=False)


# -- the measured failure ---------------------------------------------------------------------

def test_a_pinned_tool_survives_a_budget_that_the_defaults_would_have_filled(monkeypatch):
    """THE BUG. Nine slots, and the untruncated priority list wants more than nine."""
    got = _registered(monkeypatch, MCP_TOOL_MAP="1", MCP_TOOL_MAP_MAX="10",
                      MCP_TOOL_MAP_INCLUDE="list_directory", MCP_EXECUTION_PROFILES="1")
    assert "list_directory" in got, "the operator named it and it was dropped again"


def test_the_pin_outranks_the_optional_tail(monkeypatch):
    """Raising the cap alone gave the slot to list_unlocked -- 367 tokens against 168, and
    nobody asked for it."""
    got = _registered(monkeypatch, MCP_TOOL_MAP="1", MCP_TOOL_MAP_MAX="10",
                      MCP_TOOL_MAP_INCLUDE="list_directory", MCP_EXECUTION_PROFILES="1")
    assert "list_directory" in got
    assert not (NICE & set(got)), "an unrequested default took a slot ahead of the pin"


def test_the_essentials_are_never_traded_away(monkeypatch):
    """fleet_submit went in ahead of get_job_status at first, which would have broken the
    execution-profile loop silently. Both belong."""
    got = _registered(monkeypatch, MCP_TOOL_MAP="1", MCP_TOOL_MAP_MAX="10",
                      MCP_TOOL_MAP_INCLUDE="list_directory", MCP_EXECUTION_PROFILES="1")
    assert ESSENTIAL <= set(got), "missing: %s" % (ESSENTIAL - set(got))
    assert "get_job_status" in got, "the execution-profile loop lost a tool it needs"


def test_the_door_is_registered_rather_than_left_behind_the_catalogue(monkeypatch):
    """It is the tool an agent reaches for on somebody's behalf, and behind the catalogue it
    costs two extra round trips -- measured at thirteen seconds on the first real submission.

    PRESENT, not first. An earlier version asserted it sat within the first three, which put
    it ahead of the execution-profile tools and pushed get_job_status out at MAX=8. Those six
    are a protocol; a protocol missing a piece is broken, not smaller. What matters here is
    that fleet_submit is registered at all, and that it beats the optional tail.
    """
    got = _registered(monkeypatch, MCP_TOOL_MAP="1", MCP_TOOL_MAP_MAX="10",
                      MCP_TOOL_MAP_INCLUDE="list_directory", MCP_EXECUTION_PROFILES="1")
    assert "fleet_submit" in got
    assert not (NICE & set(got)), "an optional default outranked the door"


def test_the_protocol_tools_keep_their_places_at_the_shipped_minimum(monkeypatch):
    """CI's catch, pinned. At MAX=8 the six local-turn tools plus unlock and the gateway fill
    the budget exactly, and nothing may displace one of them."""
    got = _registered(monkeypatch, MCP_TOOL_MAP="1", MCP_TOOL_MAP_MAX="8",
                      MCP_EXECUTION_PROFILES="1")
    for name in ("claim_turn", "heartbeat", "commit_turn", "abort_turn",
                 "read_job_context", "get_job_status"):
        assert name in got, "%s was displaced at the shipped minimum" % name


# -- the silence, which is what made it last ----------------------------------------------------

def test_the_cap_says_what_it_discarded(monkeypatch, capsys):
    """A configuration that is ignored without a word is worse than one that is refused.

    On stderr. The first version printed to stdout and broke two tests that spawn this module
    and READ its stdout -- one as a list of names, one as JSON. A diagnostic on a data channel
    is not noise, it is corruption.
    """
    _registered(monkeypatch, MCP_TOOL_MAP="1", MCP_TOOL_MAP_MAX="4",
                MCP_TOOL_MAP_INCLUDE="list_directory", MCP_EXECUTION_PROFILES="1")
    # STDERR, deliberately: stdout is a data channel that other tests parse.
    printed = capsys.readouterr().err
    assert "[tool_map]" in printed and "cut" in printed
    assert "list_directory" in printed, "the cut tool was not named"


def test_nothing_is_said_when_nothing_is_cut(monkeypatch, capsys):
    """A line on every start would be noise, and noise is not a warning."""
    _registered(monkeypatch, MCP_TOOL_MAP="1", MCP_TOOL_MAP_MAX="70",
                MCP_TOOL_MAP_INCLUDE="list_directory", MCP_EXECUTION_PROFILES="1")
    assert "[tool_map]" not in capsys.readouterr().err


# -- shapes that must keep working ---------------------------------------------------------------

def test_no_pin_is_the_ordinary_case(monkeypatch):
    got = _registered(monkeypatch, MCP_TOOL_MAP="1", MCP_TOOL_MAP_MAX="10",
                      MCP_EXECUTION_PROFILES="1")
    assert ESSENTIAL <= set(got)


def test_a_pin_naming_something_that_does_not_exist_is_ignored_quietly(monkeypatch):
    got = _registered(monkeypatch, MCP_TOOL_MAP="1", MCP_TOOL_MAP_MAX="10",
                      MCP_TOOL_MAP_INCLUDE="no_such_tool", MCP_EXECUTION_PROFILES="1")
    assert ESSENTIAL <= set(got)
    assert "no_such_tool" not in got


def test_a_tool_is_registered_once_even_when_pinned(monkeypatch):
    """The pin list and the rest are drawn from the same set; a name in both would be sent
    twice and cost its schema twice."""
    got = _registered(monkeypatch, MCP_TOOL_MAP="1", MCP_TOOL_MAP_MAX="20",
                      MCP_TOOL_MAP_INCLUDE="list_directory", MCP_EXECUTION_PROFILES="1")
    assert len(got) == len(set(got)), "duplicate registration"
