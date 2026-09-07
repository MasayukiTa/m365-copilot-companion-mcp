# -*- coding: utf-8 -*-
"""What survives a move between machines.

The failure these pin: a copied .env that looks configured and is not. Nothing here starts a
process or reads the real .env -- the classification is the thing under test.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import env_portability as EP  # noqa: E402


def test_the_dpapi_value_is_not_carried_to_another_machine():
    """THE DEFECT THIS FILE OPENS WITH. Copied, it decrypts on neither the new account nor the
    new machine, and the failure surfaces as "not configured" at the first mutating tool --
    three days after the install looked successful."""
    assert EP.classify("MCP_UNLOCK_PASSWORD_PROTECTED") == "machine_bound"
    out = EP.merge_for_new_machine("MCP_UNLOCK_PASSWORD_PROTECTED=dpapi:AAA\nMCP_TOOL_MAP=1\n")
    assert "MCP_UNLOCK_PASSWORD_PROTECTED" not in "\n".join(out["lines"])
    assert "MCP_TOOL_MAP=1" in out["lines"]
    assert [k for k, _why in out["dropped"]] == ["MCP_UNLOCK_PASSWORD_PROTECTED"]


def test_the_tunnel_identity_does_not_travel():
    """A dev tunnel belongs to the account hosting it and cannot be renamed. Two machines
    carrying one name fight over the host, which reads as an intermittent outage."""
    for key in ("MCP_TUNNEL_NAME", "MCP_TUNNEL_URL"):
        assert EP.classify(key) == "machine_bound", key


def test_ordinary_settings_do_travel():
    """The classification has to be narrow. Dropping a working setting is the opposite failure
    and would be blamed on the transfer for a long time."""
    for key in ("MCP_TOOL_MAP", "MCP_API_KEY", "MCP_IMPL_AGENT_URL", "TASK_JOB_APPROVAL_MODE"):
        assert EP.classify(key) == "portable", key


def test_what_this_machine_already_established_wins():
    """The new machine's own values were made here, so they are valid here. Overwriting them
    with the foreign file is the failure being prevented, not a merge strategy."""
    out = EP.merge_for_new_machine("MCP_API_KEY=from-the-old-pc\n",
                                   "MCP_API_KEY=made-here\n")
    assert "MCP_API_KEY=made-here" in out["lines"]
    assert "MCP_API_KEY=from-the-old-pc" not in out["lines"]
    assert out["kept_local"] == ["MCP_API_KEY"]


def test_a_machine_bound_value_belonging_to_this_machine_is_kept():
    """Dropping is about the INCOMING file. A value this machine created for itself must
    survive the merge, or every transfer would delete the new host's own tunnel."""
    out = EP.merge_for_new_machine("MCP_TUNNEL_NAME=old-host\n", "MCP_TUNNEL_NAME=this-host\n")
    assert "MCP_TUNNEL_NAME=this-host" in out["lines"]


def test_the_flags_whose_absence_reads_as_a_different_fault_are_filled_in():
    """FLEET_INTAKE_AUTOSTART missing does not look like a config gap; it looks like the fleet
    ignoring goals. Eleven were lost that way."""
    out = EP.merge_for_new_machine("MCP_TOOL_MAP=1\n")
    joined = "\n".join(out["lines"])
    assert "FLEET_INTAKE_AUTOSTART=1" in joined
    assert "MCP_REQUIRE_UNLOCK_TOKEN=1" in joined
    assert set(out["added_defaults"]) == {"FLEET_INTAKE_AUTOSTART", "MCP_REQUIRE_UNLOCK_TOKEN"}


def test_a_default_does_not_override_a_deliberate_setting():
    """An operator who turned autostart off meant it."""
    out = EP.merge_for_new_machine("FLEET_INTAKE_AUTOSTART=0\n")
    assert "FLEET_INTAKE_AUTOSTART=0" in out["lines"]
    assert out["added_defaults"] == ["MCP_REQUIRE_UNLOCK_TOKEN"]


def test_a_repeated_key_keeps_the_last_one_the_way_dotenv_does():
    """Parsing to a dict would silently pick the first and change what the file means."""
    pairs = EP.parse_env("A=1\nA=2\n")
    assert pairs == [("A", "1"), ("A", "2")]
    assert "A=2" in EP.merge_for_new_machine("A=1\nA=2\n")["lines"]


def test_comments_and_blanks_are_not_mistaken_for_settings():
    assert EP.parse_env("# a note\n\nA=1\n") == [("A", "1")]


def test_a_present_but_undecryptable_value_is_reported_as_a_problem():
    """The class of fault a health check misses: not absent, but unusable. A missing value is
    caught at the door; this one is caught nowhere until the first mutating tool."""
    found = EP.problems({"MCP_UNLOCK_PASSWORD_PROTECTED": "dpapi:AAAAnotarealblob=="})
    assert found, "an undecryptable unlock password was not reported"
    assert found[0]["key"] == "MCP_UNLOCK_PASSWORD_PROTECTED"
    assert found[0]["problem"] == "undecryptable"


def test_a_healthy_configuration_reports_nothing():
    """The regression guard: a detector that always fires is not a detector."""
    assert EP.problems({"MCP_UNLOCK_PASSWORD": "plain-and-readable"}) == []
