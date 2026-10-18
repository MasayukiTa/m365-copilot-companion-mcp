# -*- coding: utf-8 -*-
"""relay/tool_bridge.py: the transport setting and the bridge's run_tool.

No network: the MCP client is injected (`client_factory`), and the settings file is a tmp file
named by the `path` argument, so the developer's own settings can never steer a test.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from relay import socket_tools as ST
from relay import tool_bridge as TB


# ---------------------------------------------------------------- the transport setting

def _settings(tmp_path, text):
    p = tmp_path / "settings.txt"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_the_default_is_connector_when_there_is_no_file(tmp_path):
    assert TB.transport(str(tmp_path / "missing.txt")) == TB.CONNECTOR == "connector"
    assert TB.is_bridge(str(tmp_path / "missing.txt")) is False


def test_the_default_is_connector_when_the_key_is_absent(tmp_path):
    assert TB.transport(_settings(tmp_path, "maxtabs=3\n")) == "connector"


@pytest.mark.parametrize("raw", ["connector-less", "Connector-Less", " connector-less ", "bridge", "BRIDGE"])
def test_connector_less_is_read_from_the_file_and_bridge_is_its_legacy_spelling(tmp_path, raw):
    assert TB.transport(_settings(tmp_path, "maxtabs=3\ntool_transport=%s\n" % raw)) == "connector-less"


@pytest.mark.parametrize("raw", ["", "brigde", "text_bridge", "1", "on", "connector "])
def test_an_unknown_value_falls_back_to_connector(tmp_path, raw):
    assert TB.transport(_settings(tmp_path, "tool_transport=%s\n" % raw)) == "connector"


def test_a_byte_order_mark_is_tolerated(tmp_path):
    p = tmp_path / "settings.txt"
    p.write_bytes(b"\xef\xbb\xbftool_transport=bridge\n")
    assert TB.transport(str(p)) == "connector-less"


def test_a_change_to_the_file_is_seen_without_a_restart(tmp_path):
    p = _settings(tmp_path, "tool_transport=connector\n")
    assert TB.transport(p) == "connector"
    _settings(tmp_path, "tool_transport=bridge\n")
    assert TB.transport(p) == "connector-less"


def test_no_environment_variable_can_override_the_file(tmp_path, monkeypatch):
    for name in ("MCP_TOOL_TRANSPORT", "TOOL_TRANSPORT", "MCP_FLEET_TOOL_TRANSPORT"):
        monkeypatch.setenv(name, "bridge")
    assert TB.transport(_settings(tmp_path, "tool_transport=connector\n")) == "connector"


def test_a_bridged_worker_is_one_whose_driver_carries_a_run_tool():
    assert TB.worker_is_bridged(types.SimpleNamespace(
        drv=types.SimpleNamespace(_run_tool=lambda n, a: (True, "")))) is True
    assert TB.worker_is_bridged(types.SimpleNamespace(
        drv=types.SimpleNamespace(_run_tool=None))) is False
    assert TB.worker_is_bridged(types.SimpleNamespace(drv=None)) is False
    assert TB.worker_is_bridged(types.SimpleNamespace()) is False


def test_the_bridge_limits_are_longer_than_the_connector_limits_and_settable(monkeypatch):
    from relay import relay_fleet as rf
    assert TB.idle_limit_s() > rf.SOCKET_MEANINGFUL_IDLE_S
    assert TB.turn_limit_s() >= 240
    monkeypatch.setenv("MCP_FLEET_BRIDGE_IDLE_S", "123")
    monkeypatch.setenv("MCP_FLEET_BRIDGE_TURN_S", "456")
    assert TB.idle_limit_s() == 123.0 and TB.turn_limit_s() == 456.0
    monkeypatch.setenv("MCP_FLEET_BRIDGE_IDLE_S", "junk")
    assert TB.idle_limit_s() == TB.BRIDGE_IDLE_DEFAULT_S


# ---------------------------------------------------------------- the catalogue

def test_the_catalogue_is_the_single_gateway_in_the_shape_tools_block_reads():
    cat = TB.catalogue()
    assert [t["name"] for t in cat] == ["call_tool"]
    assert cat[0]["name"] in ST.DEFAULT_ALLOWED
    block = ST.tools_block(cat)
    assert block.startswith("call_tool")
    assert "```call_tool" in block and '"arguments"' in block


# ---------------------------------------------------------------- run_tool, with a fake client

class _Block:
    def __init__(self, text):
        self.text = text


class _Result:
    def __init__(self, text="", is_error=False, n=1):
        self.content = [_Block(text)] * n
        self.is_error = is_error


class _FakeClient:
    """Stands in for fastmcp's Client: an async context manager with call_tool."""

    def __init__(self, reply=None, error=None, delay=0.0, log=None):
        self.reply, self.error, self.delay = reply, error, delay
        self.log = log if log is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def call_tool(self, name, arguments):
        self.log.append((name, arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.reply


def _factory(client):
    return lambda: client


def test_a_successful_call_returns_ok_and_the_text():
    log = []
    run = TB.make_run_tool("w3", client_factory=_factory(_FakeClient(_Result("42 rows"), log=log)))
    ok, text = run("call_tool", {"name": "count", "arguments": {"table": "t"}})
    assert (ok, text) == (True, "42 rows")
    assert log[0][0] == "call_tool"                 # always the gateway, whatever was asked
    assert log[0][1]["name"] == "count"
    assert log[0][1]["arguments"]["table"] == "t"


def test_the_worker_name_is_attached_for_the_ledger_but_never_overwrites():
    log = []
    run = TB.make_run_tool("w3", client_factory=_factory(_FakeClient(_Result("x"), log=log)))
    run("call_tool", {"name": "t", "arguments": {"a": 1}})
    assert log[-1][1]["arguments"] == {"a": 1, "_worker": "w3"}
    run("call_tool", {"name": "t", "arguments": {"_worker": "model-wrote-this"}})
    assert log[-1][1]["arguments"]["_worker"] == "model-wrote-this"


def test_the_caller_s_arguments_are_not_mutated():
    args = {"name": "t", "arguments": {"a": 1}}
    TB.make_run_tool("w3", client_factory=_factory(_FakeClient(_Result("x"))))("call_tool", args)
    assert args == {"name": "t", "arguments": {"a": 1}}


def test_a_signature_request_is_not_turned_into_a_run():
    """No `arguments` means 'show me the signature'; attaching some would execute the tool."""
    log = []
    run = TB.make_run_tool("w3", client_factory=_factory(_FakeClient(_Result("sig"), log=log)))
    run("call_tool", {"name": "read_file"})
    assert "arguments" not in log[-1][1]
    run("call_tool", {"name": ""})
    assert log[-1][1] == {"name": ""}


def test_a_tool_that_reports_an_error_is_a_failure_with_its_text():
    run = TB.make_run_tool("w", client_factory=_factory(
        _FakeClient(_Result("no such table", is_error=True))))
    assert run("call_tool", {"name": "t", "arguments": {}}) == (False, "no such table")


def test_an_exception_becomes_a_short_failure_not_a_raise():
    run = TB.make_run_tool("w", client_factory=_factory(
        _FakeClient(error=ConnectionError("refused " + "x" * 5000))))
    ok, text = run("call_tool", {"name": "t", "arguments": {}})
    assert ok is False
    assert text.startswith("ConnectionError: refused") and len(text) < 400


def test_a_client_that_cannot_even_be_built_is_a_failure_not_a_raise():
    def boom():
        raise RuntimeError("no server")
    ok, text = TB.make_run_tool("w", client_factory=boom)("call_tool", {"name": "t"})
    assert ok is False and "RuntimeError: no server" in text


def test_a_hung_call_is_cut_off_and_reported():
    run = TB.make_run_tool("w", client_factory=_factory(_FakeClient(_Result("late"), delay=5)),
                           timeout_s=0.05)
    ok, text = run("call_tool", {"name": "t", "arguments": {}})
    assert ok is False and text.startswith("TimeoutError")


def test_a_huge_result_passes_through_and_the_loop_truncates_it_and_says_so():
    big = "r" * 50000
    run = TB.make_run_tool("w", client_factory=_factory(_FakeClient(_Result(big))))
    ok, text = run("call_tool", {"name": "t", "arguments": {}})
    assert ok is True and len(text) == 50000
    turn = ST.result_turn([("call_tool", ok, text)])
    assert "切り詰め" in turn and "50000" in turn and len(turn) < 20000


def test_run_tool_works_from_inside_a_running_event_loop():
    """The turn thread has no loop, but a caller that does must not be able to break the call."""
    run = TB.make_run_tool("w", client_factory=_factory(_FakeClient(_Result("fine"))))

    async def inside():
        return run("call_tool", {"name": "t", "arguments": {}})
    assert asyncio.run(inside()) == (True, "fine")


def test_step_accepts_the_real_run_tool_end_to_end():
    run = TB.make_run_tool("w", client_factory=_factory(_FakeClient(_Result("3 files"))))
    reply = '確認します。\n```call_tool\n{"name": "ls", "arguments": {"path": "."}}\n```'
    nxt, calls = ST.step(reply, run)
    assert calls == [("call_tool", {"name": "ls", "arguments": {"path": "."}})]
    assert "3 files" in nxt and "call_tool → ok" in nxt


def test_importing_the_module_opens_no_connection_and_builds_no_client():
    import inspect
    src = inspect.getsource(TB)
    top_level_imports = [ln for ln in src.splitlines() if ln.startswith(("import ", "from "))]
    assert not any("fastmcp" in ln for ln in top_level_imports)


# ---------------------------------------------------------------- outlook is never offered

_INDEX = "\n".join([
    "3 tools, in 2 categories.",
    "  read_file(path) -- read a file",
    "  outlook_inbox(n) -- the desktop mail client's inbox",
    "  Outlook_Calendar -- calendar",
    "  count_rows -- rows in a table",
])


def test_the_catalogue_names_work_iq_and_says_outlook_is_not_available():
    text = TB.catalogue()[0]["description"]
    assert "Work IQ" in text and "outlook" in text.lower()


def test_a_listing_loses_its_outlook_lines_and_keeps_the_rest():
    log = []
    run = TB.make_run_tool("w", client_factory=_factory(_FakeClient(_Result(_INDEX), log=log)))
    ok, text = run("call_tool", {"name": ""})
    assert ok
    assert "outlook" not in text.lower()
    assert "read_file(path)" in text and "count_rows" in text


def test_a_category_listing_is_scrubbed_too():
    run = TB.make_run_tool("w", client_factory=_factory(_FakeClient(_Result(_INDEX))))
    ok, text = run("call_tool", {"name": "mail"})
    assert "outlook" not in text.lower()


@pytest.mark.parametrize("name", ["outlook_inbox", "Outlook_Send_Mail", "  outlook_calendar", "outlook"])
def test_a_call_to_an_outlook_tool_never_reaches_the_server(name):
    log = []
    run = TB.make_run_tool("w", client_factory=_factory(_FakeClient(_Result("sent"), log=log)))
    for args in ({"name": name}, {"name": name, "arguments": {"to": "x"}}):
        ok, text = run("call_tool", args)
        assert ok is False and "Work IQ" in text
    assert log == []


def test_a_tool_that_merely_mentions_outlook_is_not_blocked():
    log = []
    run = TB.make_run_tool("w", client_factory=_factory(_FakeClient(_Result("ok"), log=log)))
    ok, _ = run("call_tool", {"name": "export_for_outlook", "arguments": {}})
    assert ok and len(log) == 1


# ---------------------------------------------------------------- the command line the scripts use

def test_set_transport_writes_one_line_and_keeps_the_rest(tmp_path):
    p = _settings(tmp_path, "maxtabs=3\ntool_transport=connector\nrate_ceiling_rpm=80\n")
    assert TB.set_transport("connector-less", p) is True
    assert (tmp_path / "settings.txt").read_text(encoding="utf-8") == \
        "maxtabs=3\ntool_transport=connector-less\nrate_ceiling_rpm=80\n"
    assert TB.transport(p) == "connector-less"


def test_set_transport_appends_when_absent_and_dedups_when_repeated(tmp_path):
    p = _settings(tmp_path, "maxtabs=3\n")
    assert TB.set_transport("connector-less", p)
    assert (tmp_path / "settings.txt").read_text(encoding="utf-8").splitlines()[-1] == "tool_transport=connector-less"
    p2 = _settings(tmp_path, "tool_transport=bridge\ntool_transport=connector\n")
    assert TB.set_transport("connector", p2)
    assert (tmp_path / "settings.txt").read_text(encoding="utf-8") == "tool_transport=connector\n"


def test_set_transport_refuses_a_value_that_is_not_a_transport(tmp_path):
    p = _settings(tmp_path, "maxtabs=3\n")
    assert TB.set_transport("tunnel", p) is False
    assert (tmp_path / "settings.txt").read_text(encoding="utf-8") == "maxtabs=3\n"


def test_main_prints_the_transport(capsys, monkeypatch):
    monkeypatch.setattr(TB, "transport", lambda path=None: "connector-less")
    assert TB.main([]) == 0
    assert capsys.readouterr().out.strip() == "connector-less"
    assert TB.main(["bogus"]) == 2


# ---------------------------------------------------------------- the server's instructions

class _Init:
    def __init__(self, instructions):
        self.instructions = instructions


class _InitClient(_FakeClient):
    def __init__(self, instructions, fail=False):
        super().__init__(_Result("x"))
        self.initialize_result = None if fail else _Init(instructions)


@pytest.fixture
def fresh_instructions():
    TB._instructions_cache.clear()
    yield
    TB._instructions_cache.clear()


def test_instructions_come_from_the_initialize_result(fresh_instructions):
    assert TB.server_instructions(client_factory=_factory(_InitClient("RULE 1 ..."))) == "RULE 1 ..."


def test_instructions_are_cached_then_refreshed(fresh_instructions):
    clock = [1000.0]
    calls = []

    def factory():
        calls.append(1)
        return _InitClient("v%d" % len(calls))
    get = lambda: TB.server_instructions(client_factory=factory, now=lambda: clock[0], ttl_s=100)
    assert get() == "v1" and get() == "v1" and len(calls) == 1
    clock[0] += 101
    assert get() == "v2" and len(calls) == 2


def test_a_failed_fetch_is_empty_logged_and_not_retried_at_once(fresh_instructions, capsys):
    clock = [0.0]
    calls = []

    def factory():
        calls.append(1)
        raise ConnectionError("refused")
    get = lambda: TB.server_instructions(client_factory=factory, now=lambda: clock[0], ttl_s=600)
    assert get() == "" and get() == "" and len(calls) == 1
    assert "instructions" in capsys.readouterr().err
    clock[0] += 61
    assert get() == "" and len(calls) == 2


def test_a_stale_copy_is_kept_when_the_refresh_fails(fresh_instructions):
    clock = [0.0]
    ok = [True]

    def factory():
        if not ok[0]:
            raise ConnectionError("down")
        return _InitClient("OLD")
    get = lambda: TB.server_instructions(client_factory=factory, now=lambda: clock[0], ttl_s=10)
    assert get() == "OLD"
    ok[0] = False
    clock[0] += 11
    assert get() == "OLD"


def test_a_real_fastmcp_client_exposes_the_instructions_of_an_in_memory_server(fresh_instructions):
    """The attribute the fetch reads exists on the real client (no network: in-memory transport)."""
    fastmcp = pytest.importorskip("fastmcp")
    server = fastmcp.FastMCP("probe", instructions="RULE 1 -- from the real initialize result")
    assert TB.server_instructions(client_factory=lambda: fastmcp.Client(server)) == \
        "RULE 1 -- from the real initialize result"
