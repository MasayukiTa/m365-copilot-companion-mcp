# -*- coding: utf-8 -*-
"""The connector-less route needs no Dev Tunnel, so no setup script may require one.

THE RULE. `tool_transport=connector-less` (GUI: Settings > Tools > Tool route) means the agent holds
no connector and nothing reaches this PC through a tunnel. quickstart.bat, start_all.ps1, doctor.ps1,
supervisor.ps1 and bootstrap.py each used to treat the tunnel as part of "started" and "healthy";
each now asks relay/tool_bridge.py -- the ONE reader of the setting -- and skips the tunnel work
for connector-less. The connector route (kept by a PC set up for it, see relay/tool_bridge.py
unset_value) is left exactly as it was.

What is pinned here: that every one of those scripts asks, that the answer cannot silently become
connector-less, and that bootstrap's connector-only steps are skipped WITHOUT being marked done (so
switching back finds them pending). The scripts themselves are exercised by the existing
install-path tests; this file does not rerun them.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest import mock

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "tests"))

import bootstrap  # noqa: E402


def _read(rel):
    return (REPO / rel).read_text(encoding="utf-8-sig")


# ------------------------------------------------------------------ every script asks, once

def test_the_shared_reader_is_defined_where_all_three_scripts_dot_source_it():
    util = _read("scripts/tunnel_name_util.ps1")
    assert "function Get-ToolTransport" in util
    for rel in ("scripts/start_all.ps1", "scripts/doctor.ps1", "scripts/supervisor.ps1"):
        assert "tunnel_name_util.ps1" in _read(rel), rel
        assert "Get-ToolTransport" in _read(rel), rel


def test_the_reader_asks_the_product_and_defaults_to_connector():
    util = _read("scripts/tunnel_name_util.ps1")
    fn = util[util.index("function Get-ToolTransport"):]
    assert "tool_bridge" in fn and "transport()" in fn
    # the only value that leaves the connector route is the exact word; everything else falls through
    assert fn.count('return "connector-less"') == 1
    assert fn.rstrip().endswith('return "connector"\n}'.replace("\n", "\r\n")) or \
        fn.rstrip().endswith('return "connector"\n}')


def test_quickstart_skips_step_4_and_the_connector_values_when_connector_less():
    qs = _read("quickstart.bat").replace("\r\n", "\n")
    assert "-m relay.tool_bridge" in qs
    assert 'if "!TOOL_ROUTE!"=="connector-less" goto :route_connectorless' in qs
    # the skip comes BEFORE the access question and before the call that creates a tunnel
    skip = qs.index("goto :route_connectorless")
    assert skip < qs.index("How should Copilot Studio reach the tools on this machine?")
    assert skip < qs.index('-File "%~dp0scripts\\setup_devtunnel.ps1"')
    # the choice is offered where the access question is asked, and recorded through the product
    # L is first and recommended; A / T / N (the connector route) are the "only if you can create a connector" keys
    assert "choice /C LATN" in qs and 'if "!QS_CHOICE!"=="1" goto :route_choose_connectorless' in qs
    assert 'if "!QS_CHOICE!"=="2" set "TUNNEL_ACCESS=anonymous"' in qs
    assert 'if "!QS_CHOICE!"=="3" set "TUNNEL_ACCESS=tenant"' in qs
    assert 'if "!QS_CHOICE!"=="4" set "TUNNEL_ACCESS=none"' in qs
    assert qs.index("[L] Connector-less - RECOMMENDED") < qs.index("[A] Anonymous")
    # a PC set up for the connector route is pinned BEFORE the reader runs, so an update never moves it
    assert qs.index("-m relay.tool_bridge pin-legacy") < qs.index("do set \"TOOL_ROUTE=%%R\"")
    assert qs.isascii()
    assert "-m relay.tool_bridge set connector-less" in qs
    # ...and THIS run follows the choice at once (the reader above ran before it was made)
    choose = qs[qs.index(":route_choose_connectorless\n"):qs.index(":route_connectorless\n", qs.index(":route_choose_connectorless\n"))]
    assert 'set "TOOL_ROUTE=connector-less"' in choose
    # the connector values are not shown for it
    block = qs[qs.index('if "!TOOL_ROUTE!"=="connector-less" (\n        echo   Connector-less route'):]
    assert block.index("copilot_studio_values.ps1") > block.index(") else (")
    # every goto target is a real label
    labels = set(re.findall(r"^:(\w+)", qs, re.M))
    for tgt in re.findall(r"goto :(\w+)", qs):
        assert tgt in labels or tgt == "eof", tgt


def test_start_all_gates_each_tunnel_step_on_the_route():
    sa = _read("scripts/start_all.ps1")
    assert '$script:ToolRoute = "connector"' in sa          # the default until the reader has answered
    assert "$script:ToolRoute = Get-ToolTransport" in sa
    assert sa.index("relay.tool_bridge pin-legacy") < sa.index("$script:ToolRoute = Get-ToolTransport")
    heal = sa.index("Invoke-TunnelHealPreflight\n") if "Invoke-TunnelHealPreflight\n" in sa else \
        sa.index("Invoke-TunnelHealPreflight\r\n")
    assert 'connector-less' in sa[heal - 400:heal]            # the heal is inside the else of the route test
    assert '($script:ToolRoute -ne "connector-less") -and (Test-SupervisorTunnelDrift' in sa
    confirm = sa.index("Test-TunnelServing\n") if "Test-TunnelServing\n" in sa else sa.index("Test-TunnelServing\r\n")
    assert 'elseif ($script:ToolRoute -eq "connector-less")' in sa[confirm - 500:confirm]


def test_supervisor_still_gates_hosting_on_whether_a_tunnel_is_wanted():
    # The supervisor asks ONE question -- Test-TunnelRouteWanted -- and hosts only when it is true.
    # What makes it true changed (see below): connector-less no longer forces it false. But the
    # gate itself, and the two places that honour it, are unchanged.
    sup = _read("scripts/supervisor.ps1")
    assert "function Test-TunnelRouteWanted" in sup
    assert sup.count("Test-TunnelRouteWanted") >= 3          # definition + startup fast path + tick
    assert "if (-not (Test-TunnelRouteWanted)) {" in sup


def test_connector_less_still_hosts_a_tunnel_for_an_external_client_by_default():
    # The new default (host_tunnel_for_external_clients=on) is what keeps the tunnel up after a
    # reboot on a connector-less PC: Test-TunnelRouteWanted reads the setting and, when on, wants
    # the tunnel whatever the route is. The reason is logged so the tick that hosts it explains why.
    sup = _read("scripts/supervisor.ps1")
    assert "host_tunnel_for_external_clients_setting" in sup
    assert '$wanted = ($t -ne "connector-less") -or $hostForExternal' in sup
    assert "Dev Tunnel is hosted and watched for an external client" in sup


def test_off_restores_the_old_connector_less_behaviour():
    # With the setting off, the one condition that makes the tunnel wanted is the route being not
    # connector-less -- i.e. exactly the pre-change rule. The Python reader that supervisor.ps1
    # calls returns the connector-less default (on) unless the file says off, and off only.
    from relay import code_staleness
    assert code_staleness.HOST_TUNNEL_DEFAULT == "on"
    assert code_staleness.HOST_TUNNEL_MODES == ("off", "on")
    sup = _read("scripts/supervisor.ps1")
    # off -> $hostForExternal is false -> $wanted collapses to ($t -ne "connector-less"), the old rule
    assert '$hostForExternal = (Get-HostTunnelForExternalClientsSetting) -eq "on"' in sup


def test_doctor_replaces_the_tunnel_chain_with_one_info_row():
    doc = _read("scripts/doctor.ps1")
    start = doc.index('$script:ToolRoute = Get-ToolTransport')
    end = doc.index("# 4. Companion Edge (:9222) for the fleet/agent")
    section = doc[start:end]
    assert 'Check "tool_route"' in section and "-Info" in section
    for cid in ("tunnel_cli", "tunnel_login", "tunnel_exists", "tunnel_serving", "tunnel_supervisor_match"):
        # each connector check sits in the else branch, after the single row
        assert section.index('"%s"' % cid) > section.index("} else {")


# ------------------------------------------------------------------ bootstrap

def test_the_connector_only_steps_are_real_steps_and_not_the_always_run_ones():
    names = [n for n, _ in bootstrap.STEPS]
    for s in bootstrap.CONNECTOR_ONLY_STEPS:
        assert s in names
        assert s not in bootstrap.ALWAYS_REVALIDATE
    assert set(bootstrap.CONNECTOR_ONLY_STEPS) == {"dev_tunnel", "gen_connector"}


def _run(tmp_path, connector_less):
    calls = []
    steps = [(n, (lambda n=n: calls.append(n))) for n in ("gen_env", "dev_tunnel", "gen_connector", "verify")]
    state = {"done": {}}
    with mock.patch.object(bootstrap, "connector_less_route", return_value=connector_less), \
            mock.patch.object(bootstrap, "VENV_PYTHON", tmp_path / "no_venv" / "python.exe"):
        rc = bootstrap.run_all(steps, state, tmp_path / "state.json")
    return rc, calls, state


def test_connector_less_skips_the_tunnel_steps_without_marking_them_done(tmp_path):
    rc, calls, state = _run(tmp_path, True)
    assert rc == 0
    assert calls == ["gen_env", "verify"]
    assert "dev_tunnel" not in state["done"] and "gen_connector" not in state["done"]


def test_the_connector_route_still_runs_every_step(tmp_path):
    rc, calls, state = _run(tmp_path, False)
    assert rc == 0
    assert calls == ["gen_env", "dev_tunnel", "gen_connector", "verify"]
    assert state["done"].get("dev_tunnel") and state["done"].get("gen_connector")


def test_a_failure_to_read_the_setting_means_the_connector_route():
    from relay import tool_bridge
    with mock.patch.object(tool_bridge, "is_bridge", side_effect=OSError("settings unreadable")):
        assert bootstrap.connector_less_route() is False


# ------------------------------------------------------------------ the PowerShell reader, run

def _harness():
    import _install_path_harness as H
    return H


@pytest.mark.skipif(sys.platform != "win32", reason="runs Windows PowerShell")
def test_get_tool_transport_returns_connector_unless_the_product_says_connector_less(tmp_path):
    H = _harness()
    fn = H.ps_functions("scripts/tunnel_name_util.ps1", "Get-ToolTransport")

    def shim(name, answer):
        p = tmp_path / name
        p.write_text("@echo off\r\necho %s\r\n" % answer, encoding="ascii")
        return str(p)

    script = fn + """
'missing=' + (Get-ToolTransport -Python 'C:\\no\\such\\python.exe' -RepoRoot 'C:\\x')
'noroot=' + (Get-ToolTransport -Python '%s' -RepoRoot '')
'less=' + (Get-ToolTransport -Python '%s' -RepoRoot 'C:\\x')
'conn=' + (Get-ToolTransport -Python '%s' -RepoRoot 'C:\\x')
'junk=' + (Get-ToolTransport -Python '%s' -RepoRoot 'C:\\x')
""" % (shim("a.cmd", "connector-less"), shim("a.cmd", "connector-less"),
       shim("b.cmd", "connector"), shim("c.cmd", "bridge"))
    r = H.run_ps(script, tmp_path)
    out = dict(l.split("=", 1) for l in r.stdout.split() if "=" in l)
    assert out == {"missing": "connector", "noroot": "connector", "less": "connector-less",
                   "conn": "connector", "junk": "connector"}, (r.stdout, r.stderr)
