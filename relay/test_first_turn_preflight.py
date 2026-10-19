# -*- coding: utf-8 -*-
"""The connector-less route looks up the index and the skill match BEFORE the first model turn.

Measured 2026-10-19 (speed/測定_新経路_ターン内訳): in 20 of 23 first turns the model spent the
turn asking for the index (and skill_match), about 10 s each. These tests pin that this side
now does the lookups, that the result rides in the first prompt only, and that a failed lookup
changes nothing. No network: the MCP client is a fake.
"""
from __future__ import annotations

import asyncio
import time

from relay import socket_driver as SD
from relay import socket_tools as ST
from relay import tool_bridge as TB


class _Block:
    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self, text, is_error=False):
        self.content = [_Block(text)]
        self.is_error = is_error


class _Client:
    def __init__(self, log, delay=0.0, fail_skill=False):
        self.log, self.delay, self.fail_skill = log, delay, fail_skill

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def call_tool(self, name, arguments):
        self.log.append(dict(arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        if arguments.get("name") == "skill_match":
            if self.fail_skill:
                return _Result("boom", is_error=True)
            return _Result('{"confidence": "confident", "name": "demo"}')
        return _Result("categories:\n  files -- read\n  outlook_send -- mail")


def _pre(log, **kw):
    return TB.make_preflight("w7", client_factory=lambda: _Client(log, **kw))


# ---- the block -------------------------------------------------------------------------

def test_block_keeps_successes_and_drops_failures():
    out = ST.preflight_block([("LBL1", True, "idx"), ("LBL2", False, "err")])
    assert "idx" in out and "err" not in out and "LBL2" not in out


def test_block_is_empty_when_nothing_succeeded():
    assert ST.preflight_block([("a", False, "x"), ("b", False, "y")]) == ""
    assert ST.preflight_block([]) == ""


def test_block_truncates_a_huge_result():
    out = ST.preflight_block([("a", True, "z" * 9000)])
    assert "切り詰め" in out and len(out) < 9000


# ---- the lookups -----------------------------------------------------------------------

def test_both_lookups_run_with_the_request_and_the_worker_name():
    log = []
    text = _pre(log)("find the pptx skill")
    names = sorted(str(c.get("name")) for c in log)
    assert names == ["", "skill_match"]
    sm = [c for c in log if c.get("name") == "skill_match"][0]
    assert sm["arguments"]["text"] == "find the pptx skill"
    assert sm["arguments"]["_worker"] == "w7"
    assert "files -- read" in text and "confident" in text


def test_the_index_is_scrubbed_of_blocked_tools():
    text = _pre([])("anything")
    assert "outlook_send" not in text


def test_the_two_lookups_overlap_in_time():
    log = []
    t0 = time.time()
    _pre(log, delay=0.4)("req")
    assert time.time() - t0 < 0.75          # serial would be >= 0.8


def test_a_failed_skill_match_leaves_the_index_alone():
    text = _pre([], fail_skill=True)("req")
    assert "files -- read" in text and "boom" not in text


def test_an_empty_request_looks_up_only_the_index():
    log = []
    _pre(log)("   ")
    assert [c.get("name") for c in log] == [""]


def test_enabled_by_default_and_switchable(monkeypatch):
    monkeypatch.delenv("MCP_BRIDGE_PREFLIGHT", raising=False)
    assert TB.preflight_enabled() is True
    for off in ("0", "off", "false"):
        monkeypatch.setenv("MCP_BRIDGE_PREFLIGHT", off)
        assert TB.preflight_enabled() is False


# ---- the driver ------------------------------------------------------------------------

class _Conv:
    def __init__(self, turns=0):
        self.turns = turns


def _drv(turns=0, preflight=None, protocol="PROTO\n"):
    d = SD.CopilotSocketDriver(_Conv(turns), connect=None)
    d.use_tools([{"name": "call_tool"}], lambda n, a: (True, ""), protocol=protocol,
                preflight=preflight)
    return d


def test_first_turn_prefix_carries_protocol_then_lookups():
    seen = []
    d = _drv(preflight=lambda t: seen.append(t) or "LOOKUPS\n")
    assert d._first_turn_prefix("the goal") == "PROTO\nLOOKUPS\n"
    assert seen == ["the goal"]


def test_later_turns_get_no_prefix_and_no_lookup():
    seen = []
    d = _drv(turns=1, preflight=lambda t: seen.append(t) or "LOOKUPS\n")
    assert d._first_turn_prefix("next") == ""
    assert seen == []


def test_a_raising_lookup_costs_nothing():
    def boom(_t):
        raise RuntimeError("down")
    assert _drv(preflight=boom)._first_turn_prefix("g") == "PROTO\n"


def test_without_preflight_the_prefix_is_exactly_the_old_one():
    assert _drv()._first_turn_prefix("g") == "PROTO\n"
    assert _drv(turns=2)._first_turn_prefix("g") == ""


def test_the_lookups_land_before_the_request_in_the_prompt():
    d = _drv(preflight=lambda t: "LOOKUPS\n\n")
    prompt = ST.build_prompt("THE REQUEST", [{"name": "call_tool", "description": "d"}],
                             protocol=d._first_turn_prefix("THE REQUEST"))
    assert prompt.index("PROTO") < prompt.index("LOOKUPS") < prompt.index("THE REQUEST")
    assert prompt.rstrip().endswith("THE REQUEST")


# ---- wiring through equip --------------------------------------------------------------

def _equipped_route():
    import base64
    import json
    from relay.socket_route import SocketRoute

    class _Tpl:
        gpt_id = "T_agent.x"

        def with_deep_research_model(self, model):
            return self

    body = json.dumps({"oid": "o", "tid": "t", "exp": time.time() + 3600}).encode()
    tok = "x." + base64.urlsafe_b64encode(body).decode().rstrip("=") + ".y"
    r = SocketRoute(enabled=True, connect_fn=object(), capture_fn=lambda *a: (tok, _Tpl()))
    assert r.refresh(object(), "u")
    return TB.equip(r)


def test_connector_route_never_gets_a_preflight(monkeypatch):
    monkeypatch.setattr(TB, "transport", lambda path=None: "connector")
    assert _equipped_route().driver_for("w0")._preflight is None


def test_connector_less_route_gets_one_unless_switched_off(monkeypatch):
    monkeypatch.setattr(TB, "transport", lambda path=None: "connector-less")
    monkeypatch.setattr(TB, "server_instructions", lambda *a, **k: "")
    monkeypatch.delenv("MCP_BRIDGE_PREFLIGHT", raising=False)
    assert callable(_equipped_route().driver_for("w0")._preflight)
    monkeypatch.setenv("MCP_BRIDGE_PREFLIGHT", "0")
    assert _equipped_route().driver_for("w1")._preflight is None
