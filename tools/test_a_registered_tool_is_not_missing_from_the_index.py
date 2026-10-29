# -*- coding: utf-8 -*-
"""A tool the server registers must be in the gateway's index, or be named as left out.

THE INCIDENT (2026-10-23). A fleet worker was told to run validity_judge. It called
call_tool(name='') and read the index (203 tools, 12 categories); validity_judge was not in it.
It asked for the name anyway and was answered "no tool or category named 'validity_judge'",
concluded the tool did not exist and stopped. The server HAD registered it: tools/auto/ modules
were registered with the MCP server directly and never entered main.py's `_ALL_TOOLS`, the
table the gateway lists and dispatches from. Nothing failed anywhere, so nothing said so.

THE PROPERTY. With every optional gate open and the tool cap at the fleet's value, the names
the MCP server registers, minus the names `_ALL_TOOLS` holds, must be EXACTLY
tools.tool_catalogue.GATEWAY_UNLISTED -- each with a written reason. A newly auto-registered
tool therefore cannot drop out of the index silently: this test fails until it is listed
(the default) or named, with its reason, as left out on purpose.

Run in a child interpreter: registration happens at import, and the pytest session has already
altered what main.py would register (see relay/test_fleet_toolset.py::_catalogue).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from tools import tool_catalogue as tc

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_CHILD = r"""
import asyncio, json, sys
sys.path.insert(0, '.')
import main
from tools import tool_catalogue as tc

async def registered():
    # fastmcp 2.x exposes get_tools() (a dict); 3.x only list_tools() (a list)
    for attr in ('get_tools', 'list_tools'):
        if hasattr(main.mcp, attr):
            got = await getattr(main.mcp, attr)()
            return sorted(got if isinstance(got, dict) else [getattr(t, 'name', str(t)) for t in got])
    raise RuntimeError('no way to list the registered tools')

reg = asyncio.run(registered())
table = main._ALL_TOOLS
out = {
    "registered": reg,
    "table": sorted(table),
    "direct": sorted(getattr(t, '__name__', '') for t in main.TOOLS),
    "unlisted": {k: v for k, v in tc.GATEWAY_UNLISTED.items()},
    "validity_category": tc.render_category(table, 'validity') if 'validity' in tc.by_category(table) else '',
    "index": tc.render_index(table),
    "categories": {n: tc.categorise(n) for n in table if n.startswith('validity_')},
}
print('<<<' + json.dumps(out) + '>>>')
"""


@pytest.fixture(scope="module")
def view(tmp_path_factory):
    return _server_view(tmp_path_factory.mktemp("index_view"))


def _server_view(tmp_path):
    helper = tmp_path / "helper"
    helper.mkdir()
    (helper / "helper_entry.py").write_text("print('{}')\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("MCP_")}
    env.update({
        "MCP_API_KEY": "test-only",
        "MCP_TOOL_MAP": "1",
        "MCP_TOOL_MAP_MAX": "13",            # the value the fleet runs with
        "MCP_EXECUTION_PROFILES": "1",
        "MCP_EXT_HELPER": "1",               # gate 1 of the validity helper ...
        "MCP_EXT_HELPER_DIR": str(helper),   # ... and gate 2; both open, to see the widest set
    })
    proc = subprocess.run([sys.executable, "-c", _CHILD], cwd=REPO, env=env,
                          capture_output=True, timeout=240)
    body = proc.stdout.decode("utf-8", "replace")
    assert "<<<" in body and ">>>" in body, proc.stderr.decode("utf-8", "replace")[-3000:]
    return json.loads(body.split("<<<", 1)[1].split(">>>", 1)[0])


def test_registered_minus_indexed_is_exactly_the_written_down_exclusions(view):

    missing = sorted(set(view["registered"]) - set(view["table"]))
    assert missing == sorted(view["unlisted"]), (
        "tools the server registers but the gateway does not list: %s\n"
        "Either they should be listed (auto-registered tools are, by default) or each must be "
        "named with a reason in tools.tool_catalogue.GATEWAY_UNLISTED; the list there is %s"
        % (missing, sorted(view["unlisted"])))


def test_every_exclusion_has_a_reason_and_still_exists(view):

    for name, why in view["unlisted"].items():
        assert isinstance(why, str) and len(why.split()) >= 6, "%s: no real reason: %r" % (name, why)
        assert name in view["registered"], (
            "%s is excluded from the index but the server no longer registers it; "
            "delete the entry" % name)
        assert name not in view["table"], "%s is both excluded and listed" % name


def test_the_validity_tools_are_in_the_index_and_in_their_own_category(view):

    names = {"validity_list_claims", "validity_judge", "validity_negative_control",
             "validity_verify_lock", "validity_explain_reason", "validity_audit_ledger",
             "validity_past_runs"}
    assert names <= set(view["table"]), sorted(names - set(view["table"]))
    assert set(view["categories"].values()) == {"validity"}, view["categories"]
    for n in names:
        assert n in view["validity_category"], n
    assert "validity" in view["index"]


def test_a_tool_listed_directly_is_not_also_listed_twice(view):
    """The cap registers a few tools itself; the auto block must not register them again."""

    assert len(view["registered"]) == len(set(view["registered"]))
    assert "call_tool" in view["direct"]


def test_validity_verify_lock_is_not_filed_under_run():
    """Its name contains '_verify_', which the 'run' category's marks match."""
    assert tc.categorise("validity_verify_lock") == "validity"
    assert tc.categorise("verify_python") == "run"


def test_a_name_in_the_exclusion_list_is_not_a_tool_the_index_needs_to_say_exists():
    assert "call_tool" in tc.GATEWAY_UNLISTED
