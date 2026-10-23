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


def _env(tmp_path, text):
    p = tmp_path / ".env"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_a_new_pc_with_no_settings_file_is_connector_less(tmp_path):
    """New PC = no settings.txt and no connector footprint in .env: the route that works there."""
    missing = str(tmp_path / "missing.txt")
    assert TB.DEFAULT_TRANSPORT == TB.CONNECTOR_LESS == "connector-less"
    assert TB.transport(missing) == "connector-less"
    assert TB.transport(missing, env_path=str(tmp_path / "no.env")) == "connector-less"
    assert TB.is_bridge(missing) is True


def test_a_new_pc_with_no_tool_transport_line_is_connector_less(tmp_path):
    s = _settings(tmp_path, "maxtabs=3\n")
    assert TB.transport(s) == "connector-less"
    assert TB.transport(s, env_path=_env(tmp_path, "MCP_TUNNEL_URL=\nMCP_TUNNEL_NAME=\nFOO=bar\n")) == "connector-less"


@pytest.mark.parametrize("line", ["MCP_TUNNEL_URL=https://x.invalid/mcp", "MCP_TUNNEL_NAME=mytunnel",
                                  "MCP_TUNNEL_NAME='mytunnel'", "  MCP_TUNNEL_URL = https://x.invalid/mcp  "])
def test_a_pc_already_set_up_for_the_connector_keeps_it_when_no_line_exists(tmp_path, line):
    """An update must not switch a PC whose agent holds a connector (tunnel recorded in .env)."""
    env = _env(tmp_path, "# c\nOTHER=1\n%s\n" % line)
    assert TB.has_connector_footprint(env_path=env) is True
    assert TB.transport(str(tmp_path / "missing.txt"), env_path=env) == "connector"
    assert TB.transport(_settings(tmp_path, "maxtabs=3\n"), env_path=env) == "connector"


def test_a_commented_or_empty_tunnel_key_is_not_a_footprint(tmp_path):
    env = _env(tmp_path, "#MCP_TUNNEL_URL=https://x.invalid\nMCP_TUNNEL_URL=\nMCP_TUNNEL_NAME=''\n")
    assert TB.has_connector_footprint(env_path=env) is False


def test_a_line_in_the_file_beats_the_footprint(tmp_path):
    env = _env(tmp_path, "MCP_TUNNEL_URL=https://x.invalid/mcp\n")
    assert TB.transport(_settings(tmp_path, "tool_transport=connector-less\n"), env_path=env) == "connector-less"
    env2 = _env(tmp_path, "OTHER=1\n")
    assert TB.transport(_settings(tmp_path, "tool_transport=connector\n"), env_path=env2) == "connector"


def test_a_test_naming_only_a_settings_file_never_reads_the_developers_env(tmp_path):
    assert TB._env_file_for(str(tmp_path / "s.txt"), None) is None
    assert TB._env_file_for(None, None).endswith(".env")


def test_the_default_is_the_same_in_python_settings_keys_and_the_cockpit():
    import io
    import os
    import re
    from tools import settings_keys as SK
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cs = io.open(os.path.join(repo, "ui", "EffortPolicy.cs"), encoding="utf-8-sig").read()
    cs_default = re.search(r'class ToolRouteView.*?const string Default = "([a-z-]+)"', cs, re.S).group(1)
    assert TB.DEFAULT_TRANSPORT == SK.default("tool_transport") == cs_default == "connector-less"
    # the footprint keys are the same two words in the cockpit's UnsetValue(...) call
    cs_settings = io.open(os.path.join(repo, "ui", "FleetCockpit.Settings.cs"), encoding="utf-8-sig").read()
    for key in TB.CONNECTOR_FOOTPRINT_KEYS:
        assert 'EnvValue("%s")' % key in cs_settings


def test_pin_legacy_writes_connector_only_for_a_connector_pc_without_a_line(tmp_path):
    env = _env(tmp_path, "MCP_TUNNEL_URL=https://x.invalid/mcp\n")
    s = _settings(tmp_path, "maxtabs=3\n")
    assert TB.pin_legacy(s, env_path=env) is True
    assert (tmp_path / "settings.txt").read_text(encoding="utf-8").splitlines() == ["maxtabs=3", "tool_transport=connector"]
    assert TB.pin_legacy(s, env_path=env) is False            # already has the line: untouched
    # a new PC (no footprint) and a PC that chose connector-less are left alone
    s2 = _settings(tmp_path, "maxtabs=3\n")
    assert TB.pin_legacy(s2, env_path=_env(tmp_path, "FOO=1\n")) is False
    assert (tmp_path / "settings.txt").read_text(encoding="utf-8") == "maxtabs=3\n"
    s3 = _settings(tmp_path, "tool_transport=connector-less\n")
    assert TB.pin_legacy(s3, env_path=env) is False
    assert (tmp_path / "settings.txt").read_text(encoding="utf-8") == "tool_transport=connector-less\n"


@pytest.mark.parametrize("raw", ["connector-less", "Connector-Less", " connector-less ", "bridge", "BRIDGE"])
def test_connector_less_is_read_from_the_file_and_bridge_is_its_legacy_spelling(tmp_path, raw):
    assert TB.transport(_settings(tmp_path, "maxtabs=3\ntool_transport=%s\n" % raw)) == "connector-less"


@pytest.mark.parametrize("raw", ["", "brigde", "text_bridge", "1", "on", "connector "])
def test_an_unknown_value_reads_as_if_the_line_were_absent(tmp_path, raw):
    """A typo keeps the route the machine was on and never moves it to another one."""
    s = _settings(tmp_path, "tool_transport=%s\n" % raw)
    if raw == "connector ":          # trailing space is stripped: a real spelling
        assert TB.transport(s) == "connector"
        return
    assert TB.transport(s) == "connector-less"                                     # a new PC
    env = _env(tmp_path, "MCP_TUNNEL_NAME=t\n")
    assert TB.transport(s, env_path=env) == "connector"                            # a connector PC


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


def test_a_huge_result_passes_through_and_the_loop_sends_it_whole():
    big = "r" * 50000
    run = TB.make_run_tool("w", client_factory=_factory(_FakeClient(_Result(big))))
    ok, text = run("call_tool", {"name": "t", "arguments": {}})
    assert ok is True and len(text) == 50000
    turn = ST.result_turn([("call_tool", ok, text)])
    assert "切り詰め" not in turn and big in turn


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
    """The attribute the fetch reads exists on the real client (no network: in-memory transport).

    This asks what the client exposes, not how fast it answers, so it runs WITHOUT the fetch
    budget: the in-memory server shares the event loop with the client and its first request
    imports modules on that loop, which under load can outlast any fixed budget."""
    fastmcp = pytest.importorskip("fastmcp")
    server = fastmcp.FastMCP("probe", instructions="RULE 1 -- from the real initialize result")
    finished, text = _run_guarded(
        lambda: asyncio.run(TB._read_instructions(fastmcp.Client(server))), 300)
    assert finished, "the in-memory handshake did not finish"
    assert text == "RULE 1 -- from the real initialize result"


# ---------------------------------------------------------------- what may block, and for how long

def _run_guarded(fn, within_s):
    """Run `fn` on a daemon thread; return (finished, result). A defect that waits forever then
    fails the test at `within_s` instead of hanging the whole run."""
    import threading
    box = {}

    def _go():
        box["v"] = fn()
    t = threading.Thread(target=_go, daemon=True)
    t.start()
    t.join(within_s)
    return (not t.is_alive()), box.get("v")


class _NeverConnects(_FakeClient):
    """A server that accepts the connection and then never answers the handshake."""

    async def __aenter__(self):
        await asyncio.Event().wait()


class _YieldingInit(_InitClient):
    """Like a real client, the handshake waits on something (I/O) for a moment."""

    async def __aenter__(self):
        await asyncio.sleep(0.05)
        return self


def test_building_the_client_does_not_spend_the_fetch_budget(fresh_instructions, monkeypatch):
    """The first `fastmcp.Client` use imports a few hundred modules, synchronously, on the event
    loop. A loop that is busy cannot fire its own timer, so that work, slow on a loaded or scanned
    disk, used to be charged to the wait that is meant to bound only the server's answer."""
    import time
    monkeypatch.setattr(TB, "INSTRUCTIONS_FETCH_TIMEOUT_S", 0.2)

    def slow_to_build():
        time.sleep(0.6)                       # stands in for the cold import under load
        return _YieldingInit("RULE 1 ...")
    ok, text = _run_guarded(lambda: TB.server_instructions(client_factory=slow_to_build), 30)
    assert ok and text == "RULE 1 ..."


def test_a_server_that_never_answers_the_handshake_costs_the_budget_and_no_more(fresh_instructions, monkeypatch, capsys):
    import time
    monkeypatch.setattr(TB, "INSTRUCTIONS_FETCH_TIMEOUT_S", 0.3)
    t0 = time.time()
    ok, text = _run_guarded(lambda: TB.server_instructions(client_factory=_factory(_NeverConnects())), 30)
    assert ok and text == ""
    assert time.time() - t0 < 10
    assert "TimeoutError" in capsys.readouterr().err


def test_a_tool_call_to_a_server_that_never_answers_the_handshake_ends_at_the_limit():
    """The limit used to wrap only `call_tool`; the connect before it had no bound at all."""
    run = TB.make_run_tool("w1", client_factory=_factory(_NeverConnects()), timeout_s=0.3)
    ok, out = _run_guarded(lambda: run("call_tool", {"name": "count", "arguments": {}}), 30)
    assert ok, "run_tool is still waiting for a handshake that never comes"
    assert out[0] is False and out[1].startswith("TimeoutError")
