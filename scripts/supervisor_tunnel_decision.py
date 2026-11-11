# -*- coding: utf-8 -*-
"""Whether the supervisor should host a Dev Tunnel, from the route and one setting.

WHY THIS EXISTS (connector-less must not host)
----------------------------------------------
The supervisor used to decide "is a Dev Tunnel wanted" inline in scripts/supervisor.ps1 as
``$wanted = ($t -ne "connector-less") -or $hostForExternal`` with ``host_tunnel_for_external_clients``
defaulting ON. That meant a connector-less PC -- the default a fresh general PC gets, where the
agent reaches its tools in-process and NOTHING needs a tunnel -- still hosted and re-hosted a Dev
Tunnel after every reboot. Worse, a machine carrying a stale ``MCP_TUNNEL_NAME`` /
``MCP_TUNNEL_URL`` from a copied .env re-hosted a tunnel nobody used.

THE RULE NOW. On the connector-less route the supervisor hosts NO Dev Tunnel unless the operator
explicitly turned external-client hosting on (``host_tunnel_for_external_clients=on``). The
connector route is unchanged: it hosts a tunnel because its agent's ``tool_transport`` is
``connector``, not because of this key.

This module is the PURE decision core, the same shape the other supervisor gates use
(scripts/supervisor_host_launch.decide_host_launch, scripts/stale_server_check.decide_stale_drain):
no I/O, no clock, no process calls. The caller (Test-TunnelRouteWanted in supervisor.ps1, and the
tool_route row in doctor.ps1) measures the route and the setting and acts on the one word this
returns. relay/tool_bridge.py already resolves the route, and an explicit ``tool_transport=connector-less``
line WINS over the connector footprint there -- so a stale ``MCP_TUNNEL_NAME`` / ``MCP_TUNNEL_URL``
cannot flip an explicitly connector-less machine's route, and therefore cannot make this function
host a tunnel. ``route_is_connector_less`` below is the single place that maps a raw/aliased
``tool_transport`` value to that question, so the caller and this module cannot drift.
"""
from __future__ import annotations

CONNECTOR_LESS = "connector-less"

#: tool_transport spellings that mean the connector-less route (mirrors relay/tool_bridge._ALIASES
#: for the connector-less side). Any value not in here -- including "connector" and an unknown or
#: empty string -- is treated as NOT connector-less by route_is_connector_less, so an unreadable
#: route never silently turns tunnel hosting off for a machine that was on the connector route.
CONNECTOR_LESS_SPELLINGS = ("connector-less", "connector_less", "connectorless", "bridge")


def route_is_connector_less(tool_transport):
    """True when ``tool_transport`` names the connector-less route (case/space tolerant).

    Anything else -- "connector", "", None, or an unrecognised value -- is False, so only an
    explicit connector-less route turns hosting off; an unreadable route is treated as the
    connector route (which keeps hosting), the fail-safe direction for a machine that may already
    depend on its tunnel.
    """
    return str(tool_transport or "").strip().lower() in CONNECTOR_LESS_SPELLINGS


def decide_host_tunnel(tool_transport, host_external_setting):
    """Whether the supervisor should host/watch a Dev Tunnel. Returns "host" or "no_host".

    Inputs (both measured by the caller):

      * ``tool_transport`` -- the resolved route from relay/tool_bridge.transport()
        ("connector" or "connector-less", or an accepted alias). An explicit
        ``tool_transport=connector-less`` line already beats the connector footprint in that
        resolver, so a stale MCP_TUNNEL_NAME/MCP_TUNNEL_URL cannot reach this function as
        "connector".
      * ``host_external_setting`` -- the ``host_tunnel_for_external_clients`` setting value
        ("on" / "off"), already defaulted by code_staleness.host_tunnel_for_external_clients_setting
        (default "off").

    Rules:

      * connector route (NOT connector-less)  -> "host" always. The connector agent reaches its
        tools through the tunnel; this key does not apply. Unchanged behaviour.
      * connector-less route AND the setting is "on" -> "host". The operator explicitly asked to
        publish the local server for an external client.
      * connector-less route AND the setting is anything else (off / unknown) -> "no_host". A
        connector-less agent needs no tunnel, so none is hosted.

    Pure: no I/O, no process inspection. A missing/odd setting value reads as not "on", i.e.
    no_host on the connector-less route -- the safe direction for this change (the explicit
    default is off).
    """
    if not route_is_connector_less(tool_transport):
        return "host"
    if str(host_external_setting or "").strip().lower() == "on":
        return "host"
    return "no_host"


def host_reason(tool_transport, host_external_setting):
    """One short machine-readable reason label for the decision, for the doctor one-liner.

    * "connector"       -- connector route; the agent reaches its tools through the tunnel.
    * "external-client"  -- connector-less, but host_tunnel_for_external_clients=on.
    * "none"             -- connector-less and the setting is off: no tunnel is hosted.
    """
    if not route_is_connector_less(tool_transport):
        return "connector"
    if str(host_external_setting or "").strip().lower() == "on":
        return "external-client"
    return "none"


def doctor_line(tool_transport, host_external_setting):
    """The single human-readable line doctor prints: which tunnel mode is active and WHY.

    Shape (one line, ASCII): ``Tunnel mode: <hosted|not hosted> -- <why>``.
    """
    reason = host_reason(tool_transport, host_external_setting)
    if reason == "connector":
        return ("Tunnel mode: hosted -- tool route is connector, so the agent reaches its tools "
                "through a Dev Tunnel (host_tunnel_for_external_clients does not apply).")
    if reason == "external-client":
        return ("Tunnel mode: hosted -- tool route is connector-less, but "
                "host_tunnel_for_external_clients=on, so the supervisor hosts a Dev Tunnel for an "
                "external client.")
    return ("Tunnel mode: not hosted -- tool route is connector-less and "
            "host_tunnel_for_external_clients=off, so no Dev Tunnel is needed or hosted.")
