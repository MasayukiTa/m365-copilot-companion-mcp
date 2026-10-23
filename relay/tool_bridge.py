# -*- coding: utf-8 -*-
"""The tool transport: which way a fleet conversation reaches its tools.

User-facing name of the second transport: the CONNECTOR-LESS ROUTE (setting value `connector-less`).
"bridge" is this module's internal word for it and an accepted legacy spelling of the setting; in
this repository "the bridge" otherwise means the Copilot chat bridge, which is why the setting
uses the other name. See docs/connector_less_route.md.

TWO TRANSPORTS, ONE SETTING

  connector  The agent holds the tools itself (an MCP connector registered on the agent) and
             calls them while it answers. The behaviour that existed before this module: nothing
             here changes it. It is what a machine already set up for it keeps (see below); it
             needs a custom connector, which a new PC may not be allowed to create.

  connector-less  The agent has NO tools registered. The model asks for a call in a fenced block in
             its reply (relay/socket_tools.py), this process runs the call against its own
             local MCP server, and the result goes back in the next message. The model is a
             language model inside our loop; nothing connects outward on its behalf.

The choice is read from `tool_transport=` in settings.txt (the cockpit's file, resolved by
tools/settings_path.py) and nowhere else. There is deliberately NO environment variable for it:
an environment value that beats the screen is how a hand-passed flag silently overrides what the
operator sees (effort_policy carries that scar). Anything missing, unreadable or not one of the
two accepted spellings reads as the UNSET value, `unset_value()`:

  - a machine whose .env has MCP_TUNNEL_URL or MCP_TUNNEL_NAME (it was set up for the connector
    route) reads `connector`, so updating the code never switches it;
  - any other machine (a new PC) reads `connector-less`, the DEFAULT_TRANSPORT.

A typo is therefore "as if unset": it keeps the route the machine was on and never moves it onto
another one. `python -m relay.tool_bridge pin-legacy` writes the connector value into settings.txt for
a machine of the first kind (quickstart.bat and start_all.ps1 run it), so the cockpit and the file
say the same thing the reader does.

WHAT THE BRIDGE MODE HAS TO TURN OFF. The fleet has several checks that assume the agent holds a
connector: "the MCP connector never initialised", "the model says it has no call_tool", "a call
was printed as text instead of made", "no progress for 60 s", "no reply within 240 s". Under the
bridge none of those premises hold, so relay_fleet.py consults `worker_is_bridged()` at each of
them and relaxes or skips the check for a bridged worker only. TWO OF THE FIVE ARE REPLACED, NOT
JUST SKIPPED: a call printed as text, and the model saying it has no call_tool, are judged for a
bridged worker by relay/bridged_reply_checks.py (a call in a form the coordinator does not
execute, a wrong "no call_tool" or "tool X is not in the index"), through
`RelayWorker._bridged_reply_fault`.

NOTHING IN THIS MODULE OPENS A NETWORK CONNECTION AT IMPORT. `make_run_tool` takes a
`client_factory`, the seam through which a test supplies a fake client; the real one is built
lazily on the first call.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time

CONNECTOR = "connector"
CONNECTOR_LESS = "connector-less"
BRIDGE = CONNECTOR_LESS            # internal name kept for callers; the setting value is connector-less
TRANSPORTS = (CONNECTOR, CONNECTOR_LESS)
#: Spellings read as the connector-less route. `bridge` is what the first version of the setting
#: was called; a settings.txt written with it keeps working.
_ALIASES = {"bridge": CONNECTOR_LESS, "connector-less": CONNECTOR_LESS, "connector_less": CONNECTOR_LESS,
            "connectorless": CONNECTOR_LESS, "connector": CONNECTOR}
#: What a machine gets when settings.txt says nothing AND it has never been set up for the connector
#: route. connector-less, because a new PC (a locked-down tenant) cannot create the custom connector the
#: other route needs: with `connector` as the default, an operator who chose nothing was walked into
#: a step that cannot succeed. Must equal ui/EffortPolicy.cs ToolRouteView.Default and the
#: tool_transport default in tools/settings_keys.py (a test pins the three together).
DEFAULT_TRANSPORT = CONNECTOR_LESS

#: .env keys that exist only on a machine already set up for the connector route: bootstrap.py /
#: setup_devtunnel.ps1 write them when they create the Dev Tunnel the connector points at. A machine
#: that has either one, and no `tool_transport=` line, was running the connector route before the
#: default changed, and it keeps running it: an update must not switch it (its agent holds a
#: connector, so a switch changes what the agent does). ui/EffortPolicy.cs ToolRouteView.UnsetValue
#: reads the same two keys.
CONNECTOR_FOOTPRINT_KEYS = ("MCP_TUNNEL_URL", "MCP_TUNNEL_NAME")

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


def _env_file_for(path=None, env_path=None):
    """The .env that says whether this machine was set up for the connector route.

    A test that names its own settings file gets NO .env unless it names that too, so the developer's
    own .env can never steer it (same reason `path` exists).
    """
    if env_path is not None:
        return str(env_path)
    if path:
        return None
    from tools.settings_path import REPO
    return os.path.join(REPO, ".env")


_env_cache = {}


def has_connector_footprint(path=None, env_path=None) -> bool:
    """True when .env holds a non-empty MCP_TUNNEL_URL or MCP_TUNNEL_NAME: this machine was set up for the connector route.

    Never raises. A missing or unreadable .env is "no footprint" (a new PC has no .env, or one
    without those keys). `path` / `env_path` exist for tests.
    """
    try:
        ep = _env_file_for(path, env_path)
        if not ep:
            return False
        st = os.stat(ep)
        key = (ep, st.st_mtime_ns, st.st_size)
        hit = _env_cache.get(ep)
        if hit is not None and hit[0] == key:
            return hit[1]
        found = False
        with open(ep, encoding="utf-8-sig") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                if k.strip() in CONNECTOR_FOOTPRINT_KEYS and v.strip().strip("\"'").strip():
                    found = True
                    break
        _env_cache[ep] = (key, found)
        return found
    except Exception:
        return False


def unset_value(path=None, env_path=None) -> str:
    """What this machine runs when settings.txt has no usable `tool_transport=` line.

    connector for a machine already set up for the connector route (so an update never switches it),
    connector-less (DEFAULT_TRANSPORT) for every other machine.
    """
    return CONNECTOR if has_connector_footprint(path, env_path) else DEFAULT_TRANSPORT


def _line_value(p):
    """The transport named by the file's first `tool_transport=` line; None when there is no such
    line, the value is not an accepted spelling, or the file cannot be read."""
    try:
        st = os.stat(p)
        key = (p, st.st_mtime_ns, st.st_size)
        hit = _cache.get(p)
        if hit is not None and hit[0] == key:
            return hit[1]
        val = None
        with open(p, encoding="utf-8-sig") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln.startswith("tool_transport="):
                    val = _ALIASES.get(ln.split("=", 1)[1].strip().lower())
                    break
        _cache[p] = (key, val)
        return val
    except Exception:
        return None


def transport(path=None, env_path=None) -> str:
    """`connector` or `connector-less`, from settings.txt. Never raises.

    A `tool_transport=` line with an accepted spelling wins. Anything else -- no file, no line, an
    unreadable file, a value that is not an accepted spelling -- reads as unset_value(): the route
    this machine was already on if it was set up for the connector, otherwise the default. So a typo
    can never move a machine onto a route it was not using.

    `path` / `env_path` exist for tests (they must never be steered by the developer's own files).
    """
    try:
        p = _settings_path(path)
    except Exception:
        return unset_value(path, env_path)
    v = _line_value(p)
    return v if v is not None else unset_value(path, env_path)


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


#: Tool-name prefixes the model is never offered and never allowed to run through the gateway.
#: Owner rule: Outlook is never launched on this machine. The tools in question drive the desktop
#: mail client, so they are removed at the source (the lists the model reads and the call itself),
#: not merely discouraged in prose. Mail and calendar come from Microsoft Work IQ instead: those
#: tools are attached to the agent itself, the model calls them natively, and they never pass
#: through this module.
BLOCKED_PREFIXES = ("outlook",)


def is_blocked_tool(name) -> bool:
    """True for a tool name this route never offers or runs (case and surrounding space ignored)."""
    low = str(name or "").strip().lower()
    return any(low.startswith(p) for p in BLOCKED_PREFIXES)


def _is_discovery(payload) -> bool:
    """A gateway call that LISTS or DESCRIBES (index, category, signature) rather than runs."""
    nm = str(payload.get("name") or "").strip()
    return nm in ("", "?", "list", "*") or payload.get("arguments") is None


def scrub_listing(text) -> str:
    """Drop the lines of a gateway listing that offer a blocked tool.

    The gateway's index and category listings give one tool per line as `  name -- summary` or
    `  name(params) -- summary`. A line is dropped when its first word starts with a blocked prefix.
    Other lines (counts, category headers) are kept as they are.
    """
    out = []
    for ln in str(text).splitlines():
        word = ln.strip().split(" ", 1)[0].split("(", 1)[0]
        if word and is_blocked_tool(word):
            continue
        out.append(ln)
    return "\n".join(out)


def catalogue():
    """What the model is told it may call: the one gateway, in the `tools_block` shape."""
    return [{
        "name": "call_tool",
        "description": (
            "The gateway to every tool. Call it with name='' to list the categories, "
            "name='<category>' to open one, name='<tool>' to see that tool's parameters, and "
            "name='<tool>' with arguments to run it. "
            "WRITE A CALL AS A CODE BLOCK whose info string is call_tool and whose body is one JSON "
            "object, exactly like this (no other call syntax exists):\n"
            "```call_tool\n{\"name\": \"\", \"arguments\": {}}\n```\n"
            "For mail and calendar use the Microsoft Work IQ tools attached to this agent "
            "directly: they are not behind call_tool. Tools whose name starts with outlook "
            "are not available."),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "tool or category name"},
                "arguments": {"type": "object", "description": "the tool's arguments"},
            },
            "required": ["name"],
        },
    }]


def equip(route):
    """Make `route.driver_for` hand a bridge-mode driver its tools; return the route.

    WHY HERE AND NOT IN relay/socket_route.py. That module is in the frozen manifest
    (relay/selfimprove/frozen.py): any edit to it makes the integrity check fail until the
    owner re-baselines. The fleet already keeps transport POLICY outside it for that reason
    (see relay_fleet._socket_route), so the policy is applied here, on the one route object
    every caller shares.

    Per driver, decided when the driver is built: the setting is read then, so a change
    reaches the next conversation and a running one keeps what it started with. In `connector`
    mode the original driver_for runs and its result is returned untouched. A deep-research
    conversation (`model` given) never gets tools. A route that cannot be wrapped (a test
    double) is returned as it is.
    """
    try:
        original = route.driver_for
    except Exception:
        return route

    def driver_for(name, *args, **kwargs):
        drv = original(name, *args, **kwargs)
        if drv is None or not is_bridge():
            return drv
        model = kwargs.get("model") or (args[1] if len(args) > 1 else "")
        if model:
            return drv
        # The server's own instructions, as the protocol prefix of the FIRST turn (the driver drops
        # it on later turns). A fetch that fails gives "" and the conversation starts without it.
        text = server_instructions()
        drv.use_tools(catalogue(), make_run_tool(str(name or "")),
                      protocol=(text.rstrip() + "\n\n") if text else "",
                      preflight=make_preflight(str(name or "")) if preflight_enabled() else None)
        return drv

    try:
        route.driver_for = driver_for
    except Exception:
        return route
    return route


#: Longest the first-turn lookups may take. They are an optimisation: past this the model is
#: simply left to ask for them, as before.
PREFLIGHT_TIMEOUT_S = 20.0


def preflight_enabled() -> bool:
    """On by default for the connector-less route; MCP_BRIDGE_PREFLIGHT=0 turns it off (the A/B
    switch). Follows the module's idiom for tuning values: a default with an env override. This
    is not the transport switch -- the connector route never reaches this code."""
    return os.environ.get("MCP_BRIDGE_PREFLIGHT", "").strip().lower() not in ("0", "off", "false", "no")


def make_preflight(worker_name="", *, client_factory=None, timeout_s=None):
    """A `preflight(request_text) -> str` for one worker: the index listing and skill_match for
    the request, run HERE (in parallel, they do not depend on each other) so the first prompt
    already carries what the model would otherwise spend its first turn asking for.

    Goes through the same run_tool as a model-requested call, so the ledger attribution, the
    blocked-prefix scrub and the permission checks are identical. A failed lookup is left out."""
    from concurrent.futures import ThreadPoolExecutor
    from relay import socket_tools as ST
    run = make_run_tool(worker_name, client_factory=client_factory,
                        timeout_s=timeout_s or PREFLIGHT_TIMEOUT_S)

    def preflight(request_text):
        req = str(request_text or "").strip()[:4000]
        jobs = [("索引 (call_tool name='')", {"name": ""})]
        if req:
            jobs.append(("skill_match(依頼)", {"name": "skill_match", "arguments": {"text": req}}))
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futs = [pool.submit(run, "call_tool", args) for _label, args in jobs]
            items = []
            for (label, _args), fut in zip(jobs, futs):
                try:
                    ok, text = fut.result()
                except Exception:
                    ok, text = False, ""
                items.append((label, ok, text))
        return ST.preflight_block(items)

    return preflight


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
    """One gateway call. `timeout_s` bounds the whole exchange (connect, handshake, call), not
    only `call_tool`: a server that accepts the connection and never answers the handshake used
    to hold the turn thread for good. The client is built BEFORE the clock starts: its first use
    imports hundreds of modules synchronously, and an event loop busy doing that cannot fire its
    own timer, so counting it would charge the wait with work that has nothing to do with the
    server."""
    session = client_factory()

    async def _go():
        async with session as client:
            return await client.call_tool("call_tool", args)
    return await asyncio.wait_for(_go(), timeout_s)


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


#: The server's own `instructions` (what an MCP connector agent is handed when it connects), kept
#: here only as a cached copy fetched from the live gateway -- the text itself lives in main.py and
#: is never copied into the repository a second time.
INSTRUCTIONS_TTL_S = 600.0
INSTRUCTIONS_RETRY_S = 60.0
#: How long one fetch may wait for the server to answer. It bounds the WAIT only: building the client
#: (its first use imports several hundred modules on this thread) is done before the clock starts.
INSTRUCTIONS_FETCH_TIMEOUT_S = 10.0
_instructions_cache = {}


async def _initialize_instructions(client_factory, timeout_s=None):
    """The instructions text. `timeout_s` bounds the connect and handshake only; the client is
    built first, outside the bound (see _call)."""
    session = client_factory()
    return await asyncio.wait_for(
        _read_instructions(session),
        INSTRUCTIONS_FETCH_TIMEOUT_S if timeout_s is None else timeout_s)


async def _read_instructions(session):
    async with session as client:
        # `client.instructions` works in every protocol era; the legacy initialize result carries the
        # same text where the client has no such property.
        text = getattr(client, "instructions", None)
        if not text:
            text = getattr(getattr(client, "initialize_result", None), "instructions", "")
        return str(text or "")


def server_instructions(client_factory=None, now=None, ttl_s=None) -> str:
    """The MCP server's `instructions`, from the initialize result of the live gateway.

    A connector agent receives this text automatically; a connector-less agent does not, so it is
    sent once as the protocol prefix of a conversation's first turn (see equip). Cached for
    INSTRUCTIONS_TTL_S and then refreshed. A failed fetch returns the stale copy if there is one,
    else "" (the conversation simply starts without it), is logged to stderr, and is not retried
    for INSTRUCTIONS_RETRY_S so a down server does not cost every new conversation a timeout.
    """
    import sys
    import time
    clock = now or time.time
    ttl = INSTRUCTIONS_TTL_S if ttl_s is None else float(ttl_s)
    t = clock()
    c = _instructions_cache
    if c and t < c.get("until", 0.0):
        return c.get("text", "")
    try:
        text = _run_coroutine(_initialize_instructions(client_factory or _real_client_factory))
        c.update(text=text, until=t + ttl)
        return text
    except Exception as exc:
        sys.stderr.write("[tool_bridge] could not fetch the server instructions: %s: %s\n"
                         % (type(exc).__name__, str(exc)[:160]))
        c.setdefault("text", "")
        c["until"] = t + INSTRUCTIONS_RETRY_S
        return c["text"]


def _shadow_result_cache(payload, text, ok, duration_s, worker_name):
    """Hand one completed inner tool call to the read-only result cache in shadow. Only a
    successful call is a candidate (a failed read answers nothing a cache would serve). Never
    raises: the recorder runs inside run_tool and must not break a worker's turn."""
    try:
        if not ok:
            return
        inner = payload.get("name")
        from relay import result_cache as _rc
        if not _rc.is_read_only(inner):
            return
        _rc.shadow_observe(inner, (payload.get("arguments") or {}), text,
                           read_only=True, duration_s=duration_s, run_id=str(worker_name or ""))
    except Exception:
        return


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
            if is_blocked_tool(payload.get("name")):
                return False, ("[call_tool: no tool or category named '%s'. "
                               "For mail and calendar use the Microsoft Work IQ tools.]"
                               % str(payload.get("name"))[:60])
            _t0 = time.time()
            result = _run_coroutine(_call(factory, payload, limit))
            text = _text_of(result)
            if _is_discovery(payload):
                text = scrub_listing(text)
            ok = not _is_error(result)
            _shadow_result_cache(payload, text, ok, time.time() - _t0, worker_name)
            return ok, text
        except asyncio.TimeoutError:
            return False, "TimeoutError: the tool call took longer than %d s" % int(limit)
        except Exception as exc:
            return False, "%s: %s" % (type(exc).__name__, str(exc)[:300])

    return run_tool


# ------------------------------------------------------------------------------------------
# COMMAND LINE: the one reader and writer the setup scripts use.
#
#   python -m relay.tool_bridge            prints `connector` or `connector-less`
#   python -m relay.tool_bridge set X      writes tool_transport=X into the settings file
#   python -m relay.tool_bridge pin-legacy writes tool_transport=connector for a machine set up for
#                                          the connector route that has no such line, then prints
#
# quickstart.bat, start_all.ps1, doctor.ps1, supervisor.ps1 and bootstrap.py decide whether a Dev
# Tunnel is needed by asking THIS function, so the answer cannot differ between them and the GUI
# (which writes the same key). Nothing else parses the setting in a script.

def set_transport(value, path=None) -> bool:
    """Write `tool_transport=<value>` into the settings file, keeping every other line.

    Mirrors what the cockpit's SaveKey does: the current file is read (old or new location), the
    key's line replaced or appended, and the result written to the file the cockpit writes. Returns
    False for a value that is not one of the two transports or when the write fails.
    """
    v = _ALIASES.get(str(value or "").strip().lower())
    if v is None:
        return False
    try:
        if path:
            src = dst = str(path)
        else:
            from tools.settings_path import settings_file, settings_file_for_write
            src, dst = settings_file(), settings_file_for_write()
        lines, found = [], False
        if os.path.isfile(src):
            with open(src, encoding="utf-8-sig") as fh:
                for ln in fh.read().splitlines():
                    if ln.startswith("tool_transport="):
                        if not found:
                            lines.append("tool_transport=" + v)
                        found = True
                    else:
                        lines.append(ln)
        if not found:
            lines.append("tool_transport=" + v)
        tmp = dst + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(tmp, dst)
        _cache.pop(dst, None)
        return True
    except Exception:
        return False


def pin_legacy(path=None, env_path=None) -> bool:
    """Write `tool_transport=connector` when this machine was set up for the connector route and the
    settings file has no accepted `tool_transport=` line. True when it wrote.

    The reader already gives such a machine `connector` (unset_value); this makes the file say so,
    which is what the cockpit shows and what survives .env being edited later. A machine that
    already has a line, or has no connector footprint, is left alone.
    """
    try:
        p = _settings_path(path)
        if _line_value(p) is not None or not has_connector_footprint(path, env_path):
            return False
        return set_transport(CONNECTOR, path)
    except Exception:
        return False


def main(argv=None) -> int:
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(transport())
        return 0
    if args == ["pin-legacy"]:
        pin_legacy()
        print(transport())
        return 0
    if args[0] == "set" and len(args) == 2:
        if set_transport(args[1]):
            print(transport())
            return 0
        print("could not write tool_transport=%s" % args[1])
        return 1
    print("usage: python -m relay.tool_bridge [set connector|connector-less]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
