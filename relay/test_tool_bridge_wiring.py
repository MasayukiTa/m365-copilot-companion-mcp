# -*- coding: utf-8 -*-
"""The bridge transport reaches the driver, and the driver carries it through the loop.

(a) the route's driver_for (equipped by tool_bridge.equip; socket_route.py itself is frozen): connector builds the driver exactly as before (no catalogue, no
    run_tool); bridge hands over the gateway catalogue and a run_tool. Until now no test
    looked at what driver_for passed to the driver at all.
(e) driver -> Conversation.ask: the run_tool the driver holds is the one the loop calls, and
    the second round's payload carries its result.

No network: the socket is never opened (Conversation._one_exchange is scripted) and the MCP
client is a fake.
"""
from __future__ import annotations

import base64
import json
import time

import re

import pytest

from relay import chathub
from relay import socket_driver as SD
from relay import tool_bridge as TB
from relay.socket_route import SocketRoute

# socket_continue_recovery is ON by default (tools/settings_keys.py), so Conversation.ask
# appends a per-round ref -- "\n\n[ref:<12 hex>]" (relay/continue_recovery.ref_suffix) -- to
# every payload it puts on the wire. These tests assert on the REQUEST the model received, so
# they must look past that trailer. _wire_body strips exactly one trailing ref, leaving the
# payload a pre-recovery wire carried.
_REF_TRAILER = re.compile(r"\n\n\[ref:[0-9a-f]{12}\]\Z")


def _wire_body(payload):
    return _REF_TRAILER.sub("", payload)


@pytest.fixture(autouse=True)
def _never_write_the_real_record(tmp_path, monkeypatch):
    from relay import socket_route as SR
    monkeypatch.setattr(SR, "DEFAULT_LOG", str(tmp_path / "isolated.jsonl"))


class _Tpl:
    gpt_id = "T_agent.x"

    def with_deep_research_model(self, model):
        return self


def _token(seconds=3600):
    body = json.dumps({"oid": "o", "tid": "t", "exp": time.time() + seconds}).encode()
    return "x." + base64.urlsafe_b64encode(body).decode().rstrip("=") + ".y"


def _route():
    r = SocketRoute(enabled=True, connect_fn=object(), capture_fn=lambda *a: (_token(), _Tpl()))
    assert r.refresh(object(), "u")
    return TB.equip(r)


def _transport(monkeypatch, value):
    monkeypatch.setattr(TB, "transport", lambda path=None: value)


# ---------------------------------------------------------------- (a) driver_for

def test_connector_builds_the_driver_with_no_tools(monkeypatch):
    _transport(monkeypatch, "connector")
    drv = _route().driver_for("w0")
    assert drv is not None
    assert drv._catalogue is None
    assert drv._run_tool is None
    assert drv._protocol == ""


def test_connector_passes_exactly_the_arguments_it_always_did(monkeypatch):
    """Not just 'no tools': the constructor call itself is unchanged, so a driver class that
    does not know the newer keywords would still be constructed."""
    _transport(monkeypatch, "connector")
    seen = {}

    class _Spy:
        def __init__(self, *a, **kw):
            seen["args"], seen["kw"] = a, kw

    monkeypatch.setattr(SD, "CopilotSocketDriver", _Spy)
    _route().driver_for("w0")
    assert set(seen["kw"]) == {"connect"} and len(seen["args"]) == 1


def test_bridge_hands_the_driver_the_gateway_and_a_run_tool(monkeypatch):
    _transport(monkeypatch, "connector-less")
    drv = _route().driver_for("w7")
    assert [t["name"] for t in drv._catalogue] == ["call_tool"]
    assert callable(drv._run_tool)
    assert drv._protocol == ""                    # the goal text already carries the protocol
    assert drv.worker_name == "w7"
    assert TB.worker_is_bridged(type("W", (), {"drv": drv})())


def test_bridge_run_tool_is_bound_to_this_worker(monkeypatch):
    _transport(monkeypatch, "connector-less")
    made = []
    monkeypatch.setattr(TB, "make_run_tool", lambda name, **kw: made.append(name) or (lambda n, a: (True, "")))
    _route().driver_for("w7")
    assert made and set(made) == {"w7"}      # the run_tool, and the first-turn lookups' own


def test_a_deep_research_conversation_never_gets_tools(monkeypatch):
    _transport(monkeypatch, "connector-less")
    drv = _route().driver_for("w0", model="some-deep-model")
    assert drv._catalogue is None and drv._run_tool is None


def test_an_unset_transport_in_a_real_settings_file_follows_the_pc_s_footprint(tmp_path, monkeypatch):
    """The unpatched path: driver_for really reads the settings file (and the .env beside it)
    through the resolver. Unset = connector-less on a new PC, connector on a PC set up for it."""
    from tools import settings_path as SP
    p = tmp_path / "settings.txt"
    p.write_text("maxtabs=3\n", encoding="utf-8")
    monkeypatch.setattr(SP, "NEW_PATH", str(p))
    monkeypatch.setattr(SP, "REPO", str(tmp_path))          # the .env the resolver looks at
    assert _route().driver_for("w0")._run_tool is not None       # new PC: no .env at all
    (tmp_path / ".env").write_text("MCP_TUNNEL_URL=https://x.invalid/mcp\n", encoding="utf-8")
    assert _route().driver_for("w0")._run_tool is None           # set up for the connector: unchanged
    p.write_text("maxtabs=3\ntool_transport=bridge\n", encoding="utf-8")
    assert _route().driver_for("w0")._run_tool is not None
    p.write_text("maxtabs=3\ntool_transport=connector\n", encoding="utf-8")
    (tmp_path / ".env").write_text("OTHER=1\n", encoding="utf-8")
    assert _route().driver_for("w0")._run_tool is None           # an explicit choice always wins


def test_the_fleet_s_own_route_is_equipped(monkeypatch):
    """The wiring is not only a helper that exists: relay_fleet._socket_route() applies it, so
    a driver the fleet builds in bridge mode really carries the tools."""
    from relay import relay_fleet as rf
    from relay import socket_route as SR
    from relay import profile_token as PT
    built = []

    def fake_route(**kw):
        r = SocketRoute(enabled=True, connect_fn=object(),
                        capture_fn=lambda *a: (_token(), _Tpl()))
        r.refresh(object(), "u")
        built.append(r)
        return r

    monkeypatch.setattr(rf, "_SOCKET_ROUTE", None)
    monkeypatch.setattr(SR, "SocketRoute", fake_route)
    monkeypatch.setattr(PT, "capture_fn", lambda: (lambda *a: (_token(), _Tpl())))
    _transport(monkeypatch, "connector-less")
    route = rf._socket_route()
    assert route is built[0]
    assert route.driver_for("w1")._run_tool is not None
    _transport(monkeypatch, "connector")
    assert route.driver_for("w1")._run_tool is None      # the setting is read per driver


def test_a_route_that_cannot_be_wrapped_is_returned_as_it_is():
    marker = object()
    assert TB.equip(marker) is marker


def test_the_original_driver_for_is_what_builds_the_driver(monkeypatch):
    """equip only decorates: the connector result is the original's, the same object."""
    _transport(monkeypatch, "connector")
    sentinel = object()
    route = type("R", (), {"driver_for": lambda self, name, *a, **k: sentinel})()
    assert TB.equip(route).driver_for("w0", model="m") is sentinel
    assert TB.equip(type("R", (), {"driver_for": lambda self, n, *a, **k: None})()
                    ).driver_for("w0") is None


def test_use_tools_sets_what_the_turn_loop_forwards():
    conv, sent = _scripted_conversation(["x"])
    drv = SD.CopilotSocketDriver(conv, connect=None)
    assert drv._run_tool is None
    fn = lambda n, a: (True, "")
    drv.use_tools(TB.catalogue(), fn, protocol="")
    assert drv._run_tool is fn and drv._catalogue and drv._protocol == ""


# ---------------------------------------------------------------- (e) driver -> ask

def _scripted_conversation(replies):
    conv = chathub.Conversation(lambda: "tok")
    sent = []

    def one_exchange(payload, **kw):
        sent.append(payload)
        return replies[len(sent) - 1]

    conv._one_exchange = one_exchange
    return conv, sent


def _wait(drv):
    assert drv.wait_for_idle(timeout_s=10)


def test_the_driver_forwards_run_tool_and_the_second_round_carries_its_result():
    call = '```call_tool\n{"name": "count", "arguments": {"t": "a"}}\n```'
    conv, sent = _scripted_conversation(["調べます。\n" + call, "3件でした。"])
    ran = []

    def run_tool(name, args):
        ran.append((name, args))
        return True, "RESULT-MARKER-17"

    drv = SD.CopilotSocketDriver(conv, connect=None, catalogue=TB.catalogue(),
                                 protocol="", run_tool=run_tool)
    rounds = []
    drv.payload_sink = lambda payload, rnd: rounds.append((rnd, payload))
    drv.send("the job text")
    _wait(drv)

    assert drv.failed == ""
    assert ran == [("call_tool", {"name": "count", "arguments": {"t": "a"}})]
    assert len(sent) == 2
    assert "<tools>" in sent[0] and _wire_body(sent[0]).endswith("the job text")  # round 0
    assert "RESULT-MARKER-17" in sent[1] and "call_tool → ok" in sent[1]  # round 1
    assert [r for r, _ in rounds] == [0, 1]
    assert drv.read_last_response() == "3件でした。"                        # no raw block upstream


def test_a_failing_tool_is_reported_to_the_model_and_the_turn_still_ends():
    call = '```call_tool\n{"name": "count", "arguments": {}}\n```'
    conv, sent = _scripted_conversation([call, "了解、別の方法を使います。"])
    drv = SD.CopilotSocketDriver(conv, connect=None, catalogue=TB.catalogue(), protocol="",
                                 run_tool=lambda n, a: (False, "ConnectionError: refused"))
    drv.send("job")
    _wait(drv)
    assert drv.failed == ""
    assert "失敗" in sent[1] and "ConnectionError: refused" in sent[1]


def test_without_a_run_tool_the_driver_asks_once_and_nothing_is_executed():
    """The connector shape: no catalogue, no run_tool -> one round, the wire text is the job."""
    conv, sent = _scripted_conversation(["done"])
    drv = SD.CopilotSocketDriver(conv, connect=None)
    drv.send("the job text")
    _wait(drv)
    assert len(sent) == 1 and _wire_body(sent[0]) == "the job text"


# ---------------------------------------------------------------- (e) no tunnel in the path

def test_connector_less_does_not_probe_a_tunnel_left_over_in_env(monkeypatch):
    from relay import mcp_ready
    monkeypatch.setenv("MCP_TUNNEL_URL", "https://example.invalid/mcp")
    probed = []
    monkeypatch.setattr(mcp_ready, "fetch_json", lambda url, timeout_s=3.0: probed.append(url))
    _transport(monkeypatch, "connector-less")
    assert mcp_ready.default_tunnel(123) == (True, "")
    assert probed == []
    _transport(monkeypatch, "connector")
    ok, why = mcp_ready.default_tunnel(123)
    assert ok is False and probed and probed[0].startswith("https://example.invalid")


def test_infra_health_asks_only_the_local_server_when_connector_less(monkeypatch):
    from relay import relay_fleet
    monkeypatch.setenv("MCP_TUNNEL_URL", "https://example.invalid/mcp")
    asked = []
    monkeypatch.setattr(relay_fleet, "_url_ok", lambda url, timeout_s=3.0: asked.append(url) or True)
    _transport(monkeypatch, "connector-less")
    assert relay_fleet._infra_healthy() is True
    assert asked == ["http://127.0.0.1:8000/health"]
    asked.clear()
    _transport(monkeypatch, "connector")
    assert relay_fleet._infra_healthy() is True
    assert len(asked) == 2


# ---------------------------------------------------------------- (f) the server's own instructions

def test_the_instructions_ride_on_the_first_turn_only():
    """A connector agent is handed the server's instructions when it connects; the connector-less
    agent gets them as the protocol prefix of the conversation's FIRST turn and never again."""
    conv, sent = _scripted_conversation(["one", "two"])
    drv = SD.CopilotSocketDriver(conv, connect=None, catalogue=TB.catalogue(), protocol="SERVER-RULES\n\n",
                                 run_tool=lambda n, a: (True, ""))
    drv.send("first job")
    _wait(drv)
    drv.send("second job")
    _wait(drv)
    assert sent[0].startswith("SERVER-RULES") and _wire_body(sent[0]).endswith("first job")
    assert "SERVER-RULES" not in sent[1] and _wire_body(sent[1]).endswith("second job")


def test_equip_hands_the_fetched_instructions_to_a_connector_less_driver(monkeypatch):
    monkeypatch.setattr(TB, "server_instructions", lambda *a, **k: "RULE 1 -- call call_tool first")
    _transport(monkeypatch, "connector-less")
    drv = _route().driver_for("w0")
    assert drv._protocol.startswith("RULE 1 -- call call_tool first")
    _transport(monkeypatch, "connector")
    assert _route().driver_for("w0")._protocol == ""        # the connector route is untouched


def test_a_failed_fetch_leaves_the_protocol_empty(monkeypatch):
    monkeypatch.setattr(TB, "server_instructions", lambda *a, **k: "")
    _transport(monkeypatch, "connector-less")
    assert _route().driver_for("w0")._protocol == ""
