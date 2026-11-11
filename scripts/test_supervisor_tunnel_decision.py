# -*- coding: utf-8 -*-
"""The pure host/no-host decision for the Dev Tunnel (scripts/supervisor_tunnel_decision.py).

THE RULE being pinned. On the connector-less route the supervisor hosts NO Dev Tunnel unless the
operator turned host_tunnel_for_external_clients on; the connector route always hosts. This file
exercises the pure core directly -- the same function supervisor.ps1's Test-TunnelRouteWanted and
doctor.ps1 call -- so the matrix (route x setting) and the fail-safe directions are nailed down in
one place, with no PowerShell and no I/O.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import supervisor_tunnel_decision as d  # noqa: E402


# ------------------------------------------------------------------ the core matrix

def test_connector_less_off_does_not_host():
    # The whole point of the change: a standard, connector-less PC with the default setting hosts
    # nothing.
    assert d.decide_host_tunnel("connector-less", "off") == "no_host"
    assert d.host_reason("connector-less", "off") == "none"


def test_connector_less_on_hosts_for_an_external_client():
    assert d.decide_host_tunnel("connector-less", "on") == "host"
    assert d.host_reason("connector-less", "on") == "external-client"


def test_connector_route_always_hosts_whatever_the_setting():
    for setting in ("off", "on", "", None, "garbage"):
        assert d.decide_host_tunnel("connector", setting) == "host", setting
        assert d.host_reason("connector", setting) == "connector", setting


# ------------------------------------------------------------------ fail-safe directions

def test_unknown_or_empty_route_is_treated_as_connector_and_keeps_hosting():
    # An unreadable route must NOT silently turn hosting off for a machine that may already depend
    # on its tunnel: anything not recognised as connector-less is the connector side.
    for route in ("", None, "connector", "CONNECTOR", "weird", "tunnel"):
        assert d.decide_host_tunnel(route, "off") == "host", route


def test_connector_less_spellings_are_all_recognised():
    for route in ("connector-less", "connector_less", "connectorless", "CONNECTOR-LESS",
                  "  connector-less  ", "bridge"):
        assert d.route_is_connector_less(route) is True, route
        # and therefore host nothing when the setting is off
        assert d.decide_host_tunnel(route, "off") == "no_host", route


def test_setting_value_is_case_and_space_tolerant_but_only_on_hosts():
    assert d.decide_host_tunnel("connector-less", "ON") == "host"
    assert d.decide_host_tunnel("connector-less", "  on ") == "host"
    # anything that is not on is no_host on the connector-less route
    for setting in ("off", "", None, "yes", "1", "true", "enabled"):
        assert d.decide_host_tunnel("connector-less", setting) == "no_host", setting


# ------------------------------------------------------------------ stale-env resilience

def test_an_explicit_connector_less_route_cannot_be_flipped_by_a_stale_env():
    # The decider is handed the ALREADY-RESOLVED route. relay/tool_bridge.py resolves an explicit
    # tool_transport=connector-less ahead of any connector footprint, so a stale
    # MCP_TUNNEL_NAME/MCP_TUNNEL_URL from a copied .env never reaches this function as "connector".
    # Pin the consequence: given "connector-less", no setting value except on can make it host, and
    # the env is not an input here at all.
    assert d.decide_host_tunnel("connector-less", "off") == "no_host"
    # the function takes exactly two inputs; there is no third door for an env var to open
    import inspect
    params = list(inspect.signature(d.decide_host_tunnel).parameters)
    assert params == ["tool_transport", "host_external_setting"]


# ------------------------------------------------------------------ the doctor one-liner

def test_doctor_line_is_one_line_and_states_mode_and_why():
    for route, setting, hosted in (
        ("connector", "off", True),
        ("connector-less", "on", True),
        ("connector-less", "off", False),
    ):
        line = d.doctor_line(route, setting)
        assert "\n" not in line and "\r" not in line
        assert line.startswith("Tunnel mode: ")
        assert " -- " in line  # a reason follows the mode
        if hosted:
            assert "hosted" in line and "not hosted" not in line
        else:
            assert "not hosted" in line
    # the three reasons are distinct so the reader can tell WHY
    lines = {
        d.doctor_line("connector", "off"),
        d.doctor_line("connector-less", "on"),
        d.doctor_line("connector-less", "off"),
    }
    assert len(lines) == 3


def test_doctor_line_for_connector_less_off_names_the_setting_and_route():
    line = d.doctor_line("connector-less", "off").lower()
    assert "connector-less" in line
    assert "host_tunnel_for_external_clients" in line
    assert "not hosted" in line
