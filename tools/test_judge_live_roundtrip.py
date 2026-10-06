# -*- coding: utf-8 -*-
"""One real round trip, through the registration the deployment actually uses.

WHY THIS FILE EXISTS. Everything else about this layer is tested against injected fakes, and
the one thing a fake cannot check is the part I was least sure of: shell_exec is SYNCHRONOUS
and Context.elicit is a COROUTINE. Whether one can reach the other is a fact about two
libraries, not about my code, and a source assertion cannot execute.

(This file used to prove the same bridge for Context.sample as well. fastmcp 4 removed
Context.sample and production never ran the sampling backend, so only the elicitation half
remains; the thread bridge is the same machinery either way.)

WHY IT USES register(). The first version of this file built its server with a bare
`mcp.tool()(fn)`, and every round trip failed -- NoEventLoopError, then a twenty-second
timeout. I concluded, and committed, that a sync tool structurally cannot make an outbound MCP
request, and that every sync tool blocks the server's event loop.

Both conclusions were wrong, for the same reason: main.py does not register tools that way. It
registers `register(tool)`, and tools/registry.py's register() already wraps every tool in
`anyio.to_thread.run_sync` -- precisely so a slow tool cannot freeze the loop. Under that
wrapper the tool runs in an anyio worker thread, `anyio.from_thread.run` has its token, and the
round trip works. Measured: `thread=AnyIO worker thread`, and the answer came back.

So the test server had a shape the deployment does not have, and what it measured was a
configuration that does not exist -- the same defect as a stub written to agree with the code.
Every server built here therefore goes through register(), and the first test asserts which
thread the tool lands on, so a change to that wrapper fails here rather than silently disabling
the question.

WHY `wrap=False` ALSO PASSES `run_in_thread=False` (2026-09-24). fastmcp used to run a bare
`mcp.tool()(fn)` sync function INLINE on the loop thread. fastmcp 3.4.7 changed that default:
`FunctionTool.run_in_thread` now defaults to `True` even for a bare registration, so reproducing
"runs on the loop thread" takes an explicit ask. The defect this file pins -- a sync tool
running ON the loop thread cannot make an outbound request, and must refuse in milliseconds
rather than deadlock -- is still real; `run_in_thread=False` asks fastmcp for that shape
explicitly, which is also fastmcp's own documented escape hatch for callers with thread-affinity
requirements.

In-memory transport: no network, no port, no browser, no Copilot. It proves the plumbing, not
that the production client can do any of this -- that client declares its own capabilities and
this file cannot speak for it.
"""
import pytest

from fastmcp import Client, FastMCP

from tools import judge_backend as B
from tools.registry import register

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _server(fn, wrap=True):
    """A server built the way main.py builds one.

    `wrap=False` reproduces the mistake above, and one test uses it deliberately to pin the
    difference -- so if register() ever stops offloading, the reason this file cares is already
    written down beside the failure.
    """
    mcp = FastMCP("judge-roundtrip-test")
    # The deployment installs this too; without it there is no loop to fall back on.
    assert B.install(mcp), "the loop-capture middleware could not be installed"
    if wrap:
        mcp.tool()(register(fn))
    else:
        mcp.tool(run_in_thread=False)(fn)
    return mcp


async def _call(server, name, args, **client_kw):
    # fastmcp 4's client negotiates the 2026-07-28 protocol era by default, where a server cannot
    # push an elicitation request mid-call. The deployment's clients still use the initialize
    # handshake, so the round trips below pin it; the modern-era behaviour has its own test.
    client_kw.setdefault("mode", "legacy")
    async with Client(server, **client_kw) as client:
        res = await client.call_tool(name, args)
    return "".join(getattr(c, "text", "") for c in res.content)


# ── the round trip that matters ───────────────────────────────────────────────────────────

async def test_a_registered_sync_tool_reaches_the_person():
    """THE BRIDGE, EXERCISED, in the shape shell_exec is actually deployed in."""
    seen = {}

    def asking_probe(question: str) -> str:
        """probe"""
        seen["on_loop"] = B.on_the_event_loop_thread()
        return repr(B.ask_human(question))

    async def elicit_handler(message, response_type, params, ctx):
        seen["message"] = message
        from fastmcp.client.elicitation import ElicitResult
        return ElicitResult(action="accept", content={"value": True})

    text = await _call(_server(asking_probe), "asking_probe", {"question": "may I delete build/?"},
                       elicitation_handler=elicit_handler)

    assert seen["on_loop"] is False, (
        "the tool ran ON the event loop; register()'s to_thread offload has changed, and "
        "without it no outbound MCP request can be made from a sync tool")
    assert seen["message"] == "may I delete build/?"
    assert text == "True", "the person's answer did not come back: %r" % text


async def test_an_unwrapped_tool_cannot_and_says_why():
    """THE MISTAKE, PINNED. Registered without register(), the tool runs on the loop thread and
    an outbound request deadlocks -- which is what the first version of this file measured and
    mistook for a property of fastmcp. The refusal must be immediate: a twenty-second stall in
    front of every judged command would make the layer unusable while looking configured.
    """
    out = {}

    def asking_probe(question: str) -> str:
        """probe"""
        import time
        t0 = time.time()
        out["on_loop"] = B.on_the_event_loop_thread()
        out["answer"] = B.ask_human(question)
        out["elapsed"] = time.time() - t0
        return "done"

    async def elicit_handler(message, response_type, params, ctx):
        from fastmcp.client.elicitation import ElicitResult
        return ElicitResult(action="accept", content={"value": True})

    await _call(_server(asking_probe, wrap=False), "asking_probe", {"question": "may I?"},
                elicitation_handler=elicit_handler)

    assert out["on_loop"] is True
    assert out["answer"] is None, "on the loop thread nobody can be asked, and None is not approval"
    assert out["elapsed"] < 2.0, \
        "refused in %.1fs; it must not wait out a timeout" % out["elapsed"]


# ── what the client declared ──────────────────────────────────────────────────────────────

async def test_the_capability_check_sees_a_client_that_can_elicit():
    reach = {}

    def probe() -> str:
        """probe"""
        reach.update(B.availability())
        return "ok"

    async def elicit_handler(message, response_type, params, ctx):
        from fastmcp.client.elicitation import ElicitResult
        return ElicitResult(action="accept", content={"value": True})

    await _call(_server(probe), "probe", {}, elicitation_handler=elicit_handler)
    assert reach["in_request"] is True
    assert reach["client_elicitation"] is True


async def test_a_client_with_no_elicitation_handler_is_reported_as_nobody_to_ask():
    """The half that must not read as "allowed"."""
    out = {}

    def probe() -> str:
        """probe"""
        out.update(B.availability())
        out["answer"] = B.ask_human("may I?")
        return "ok"

    await _call(_server(probe), "probe", {})
    assert out["in_request"] is True
    assert out["client_elicitation"] is False
    assert out["answer"] is None, "a client that cannot be asked must not silently approve"


# ── the person ────────────────────────────────────────────────────────────────────────────

async def test_the_person_can_be_asked_and_their_approval_is_carried_back():
    asked = {}

    def probe(question: str) -> str:
        """probe"""
        return repr(B.ask_human(question))

    async def elicit_handler(message, response_type, params, ctx):
        asked["message"] = message
        from fastmcp.client.elicitation import ElicitResult
        return ElicitResult(action="accept", content={"value": True})

    text = await _call(_server(probe), "probe", {"question": "may I delete build/?"},
                       elicitation_handler=elicit_handler)
    assert asked["message"] == "may I delete build/?"
    assert text == "True"


async def test_a_declining_person_is_not_an_approval():
    def probe(question: str) -> str:
        """probe"""
        return repr(B.ask_human(question))

    async def elicit_handler(message, response_type, params, ctx):
        from fastmcp.client.elicitation import ElicitResult
        return ElicitResult(action="decline", content=None)

    text = await _call(_server(probe), "probe", {"question": "may I?"},
                       elicitation_handler=elicit_handler)
    assert text in ("False", "None"), "a decline must never read as True; got %r" % text


async def test_a_modern_era_client_cannot_be_asked_and_that_is_not_an_approval():
    """On a 2026-07-28 connection ctx.elicit() raises (no server-initiated requests). ask_human
    must read that as "nobody could be asked" (None), never as a yes."""
    def probe(question: str) -> str:
        """probe"""
        return repr(B.ask_human(question))

    async def elicit_handler(message, response_type, params, ctx):
        from fastmcp.client.elicitation import ElicitResult
        return ElicitResult(action="accept", content={"value": True})

    text = await _call(_server(probe), "probe", {"question": "may I?"},
                       elicitation_handler=elicit_handler, mode="auto")
    assert text == "None", "a connection that cannot carry the question must not approve: %r" % text


async def test_a_client_with_no_elicitation_cannot_approve_anything():
    def probe(question: str) -> str:
        """probe"""
        return "%r %r" % (B.human_available(), B.ask_human(question))

    text = await _call(_server(probe), "probe", {"question": "may I?"})
    assert text.startswith("False"), "human_available must be False; got %r" % text
    assert "True" not in text, "no approval may come from a client that cannot ask"
