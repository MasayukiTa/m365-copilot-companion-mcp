# -*- coding: utf-8 -*-
"""The tool transport: which way a fleet conversation reaches its tools.

TWO TRANSPORTS, ONE SETTING

  connector  The agent holds the tools itself (an MCP connector registered on the agent) and
             calls them while it answers. This is the default and the behaviour that existed
             before this module: nothing here changes it.

  bridge     The agent has NO tools registered. The model asks for a call in a fenced block in
             its reply (relay/socket_tools.py), this process runs the call against its own
             local MCP server, and the result goes back in the next message. The model is a
             language model inside our loop; nothing connects outward on its behalf.

The choice is read from `tool_transport=` in settings.txt (the cockpit's file, resolved by
tools/settings_path.py) and nowhere else. There is deliberately NO environment variable for it:
an environment value that beats the screen is how a hand-passed flag silently overrides what the
operator sees (effort_policy carries that scar). Anything missing, unreadable or not one of the
two words reads as `connector`, so a typo can never switch a machine onto the unproven path.

WHAT THE BRIDGE MODE HAS TO TURN OFF. The fleet has several checks that assume the agent holds a
connector: "the MCP connector never initialised", "the model says it has no call_tool", "a call
was printed as text instead of made", "no progress for 60 s", "no reply within 240 s". Under the
bridge none of those premises hold, so relay_fleet.py consults `worker_is_bridged()` at each of
them and relaxes or skips the check for a bridged worker only.

NOTHING IN THIS MODULE OPENS A NETWORK CONNECTION AT IMPORT. `make_run_tool` takes a
`client_factory`, the seam through which a test supplies a fake client; the real one is built
lazily on the first call.
"""
from __future__ import annotations

import asyncio
import os
import threading

CONNECTOR = "connector"
BRIDGE = "bridge"
TRANSPORTS = (CONNECTOR, BRIDGE)
DEFAULT_TRANSPORT = CONNECTOR

#: The local MCP server the bridge calls. The gateway tool lives behind this one URL.
LOCAL_MCP_URL = "http://127.0.0.1:8000/mcp"

#: Longest one tool call may run before the bridge reports a failure to the model instead of
#: waiting for it. Bounded so a hung call ends in a message the model can act on, not in the
#: fleet's own watchdog killing the whole conversation.
CALL_TIMEOUT_S = 600.0

#: Bridged turns include tool round trips, so the fleet's clocks are longer for them. Both are
#: tuning values (not the mode switch) and follow the repository's existing idiom for such
#: values: a module default with an environment override.
BRIDGE_IDLE_DEFAULT_S = 600.0
BRIDGE_TURN_DEFAULT_S = 1200.0

#: (path, mtime_ns, size) -> value. One os.stat per read, a re-read only when the file changed,
#: so a change made in the cockpit is seen by the NEXT conversation without a restart.
_cache = {}


def _settings_path(path=None):
    if path:
        return str(path)
    from tools.settings_path import settings_file
    return settings_file()


def transport(path=None) -> str:
    """`connector` or `bridge`, from settings.txt. Never raises; anything unclear is connector.

    `path` exists for tests (they must never be steered by the developer's own settings file).
    """
    try:
        p = _settings_path(path)
        st = os.stat(p)
        key = (p, st.st_mtime_ns, st.st_size)
        hit = _cache.get(p)
        if hit is not None and hit[0] == key:
            return hit[1]
        val = DEFAULT_TRANSPORT
        with open(p, encoding="utf-8-sig") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln.startswith("tool_transport="):
                    v = ln.split("=", 1)[1].strip().lower()
                    val = v if v in TRANSPORTS else DEFAULT_TRANSPORT
                    break
        _cache[p] = (key, val)
        return val
    except Exception:
        return DEFAULT_TRANSPORT


def is_bridge(path=None) -> bool:
    return transport(path) == BRIDGE


def worker_is_bridged(worker) -> bool:
    """True when this worker's driver was given a `run_tool`, i.e. it is a bridged conversation.

    Decided from the DRIVER, not from the setting, on purpose: the setting can change while a
    run is going, and a conversation that was started as a connector conversation has its
    connector for as long as it lives.
    """
    try:
        drv = getattr(worker, "drv", None)
        return drv is not None and getattr(drv, "_run_tool", None) is not None
    except Exception:
        return False


def _env_seconds(name, default):
    try:
        v = float(os.environ.get(name, "") or default)
        return v if v > 0 else float(default)
    except (TypeError, ValueError):
        return float(default)


def idle_limit_s() -> float:
    """Meaningful-idle limit for a bridged socket turn (a local tool may run silently)."""
    return _env_seconds("MCP_FLEET_BRIDGE_IDLE_S", BRIDGE_IDLE_DEFAULT_S)


def turn_limit_s() -> float:
    """The outer one-turn clock for a bridged conversation (tool round trips included)."""
    return _env_seconds("MCP_FLEET_BRIDGE_TURN_S", BRIDGE_TURN_DEFAULT_S)


def catalogue():
    """What the model is told it may call: the one gateway, in the `tools_block` shape."""
    return [{
        "name": "call_tool",
        "description": (
            "The gateway to every tool. Call it with name='' to list the categories, "
            "name='<category>' to open one, name='<tool>' to see that tool's parameters, and "
            "name='<tool>' with arguments to run it."),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "tool or category name"},
                "arguments": {"type": "object", "description": "the tool's arguments"},
            },
            "required": ["name"],
        },
    }]


def _real_client_factory():
    """A fastmcp Client for the local server, built lazily (the only network seam)."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from tools.secret_store import api_key_from_env
    headers = {}
    key = api_key_from_env()
    if key:
        headers["Authorization"] = key
    return Client(StreamableHttpTransport(url=LOCAL_MCP_URL, headers=headers))


def _text_of(result) -> str:
    """The text of a tool result: its content blocks joined, whatever the client returned."""
    content = getattr(result, "content", None)
    if content is None and isinstance(result, (list, tuple)):
        content = result
    if content is None:
        return str(result)
    return "".join(str(getattr(b, "text", "") or "") for b in content)


def _is_error(result) -> bool:
    return bool(getattr(result, "is_error", False))


async def _call(client_factory, args, timeout_s):
    async with client_factory() as client:
        return await asyncio.wait_for(client.call_tool("call_tool", args), timeout_s)


def _run_coroutine(coro):
    """Run a coroutine to completion from sync code, whether or not a loop is already running
    in this thread (the turn thread has none, but a caller must not be able to break us)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box = {}

    def _go():
        try:
            box["v"] = asyncio.run(coro)
        except BaseException as exc:          # re-raised in the caller's thread
            box["e"] = exc
    t = threading.Thread(target=_go, name="tool-bridge-call", daemon=True)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box["v"]


def make_run_tool(worker_name="", *, client_factory=None, timeout_s=None):
    """A `run_tool(name, args) -> (ok, text)` for one worker's driver.

    `name` is the fenced block's info string (only `call_tool` ever gets here, see
    socket_tools.DEFAULT_ALLOWED) and `args` the block's JSON object: {"name": ..., "arguments":
    {...}}. The call goes to the server's own gateway, so the ledger, the unlock gate and the
    per-tool permission checks apply exactly as they do for a connector call.

    The worker's name is injected as `_worker` so the tool ledger can attribute the call to
    this conversation (the gateway strips it before dispatch); a value the model wrote itself
    is never overwritten. Every failure -- including a timeout -- comes back as
    (False, one short line): the model can often recover, and the turn must not die on it.
    """
    factory = client_factory or _real_client_factory
    limit = float(timeout_s) if timeout_s else CALL_TIMEOUT_S

    def run_tool(name, args):
        try:
            payload = dict(args or {})
            inner = payload.get("arguments")
            if isinstance(inner, dict) and worker_name and "_worker" not in inner:
                payload["arguments"] = dict(inner, _worker=str(worker_name))
            # No `arguments` at all means "show me the signature": adding one would turn that
            # into a run, so attribution is attached only to a call that already has them.
            result = _run_coroutine(_call(factory, payload, limit))
            return (not _is_error(result)), _text_of(result)
        except asyncio.TimeoutError:
            return False, "TimeoutError: the tool call took longer than %d s" % int(limit)
        except Exception as exc:
            return False, "%s: %s" % (type(exc).__name__, str(exc)[:300])

    return run_tool
