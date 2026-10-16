# -*- coding: utf-8 -*-
"""The bridge transport reaches the driver, and the driver carries it through the loop.

(a) socket_route.driver_for: connector builds the driver exactly as before (no catalogue, no
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

import pytest

from relay import chathub
from relay import socket_driver as SD
from relay import tool_bridge as TB
from relay.socket_route import SocketRoute


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
    return r


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
    _transport(monkeypatch, "bridge")
    drv = _route().driver_for("w7")
    assert [t["name"] for t in drv._catalogue] == ["call_tool"]
    assert callable(drv._run_tool)
    assert drv._protocol == ""                    # the goal text already carries the protocol
    assert drv.worker_name == "w7"
    assert TB.worker_is_bridged(type("W", (), {"drv": drv})())


def test_bridge_run_tool_is_bound_to_this_worker(monkeypatch):
    _transport(monkeypatch, "bridge")
    made = []
    monkeypatch.setattr(TB, "make_run_tool", lambda name, **kw: made.append(name) or (lambda n, a: (True, "")))
    _route().driver_for("w7")
    assert made == ["w7"]


def test_a_deep_research_conversation_never_gets_tools(monkeypatch):
    _transport(monkeypatch, "bridge")
    drv = _route().driver_for("w0", model="some-deep-model")
    assert drv._catalogue is None and drv._run_tool is None


def test_an_unset_transport_in_a_real_settings_file_means_connector(tmp_path, monkeypatch):
    """The unpatched path: driver_for really reads the settings file through the resolver."""
    from tools import settings_path as SP
    p = tmp_path / "settings.txt"
    p.write_text("maxtabs=3\n", encoding="utf-8")
    monkeypatch.setattr(SP, "NEW_PATH", str(p))
    assert _route().driver_for("w0")._run_tool is None
    p.write_text("maxtabs=3\ntool_transport=bridge\n", encoding="utf-8")
    assert _route().driver_for("w0")._run_tool is not None


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
    assert "<tools>" in sent[0] and sent[0].endswith("the job text")      # round 0
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
    assert sent == ["the job text"]
