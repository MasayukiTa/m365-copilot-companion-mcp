# -*- coding: utf-8 -*-
"""One real round trip, and the fact it uncovered.

WHY THIS FILE HAD TO EXIST. Everything else about this layer is tested against injected fakes,
and the one thing a fake cannot check is the part I was least sure of: shell_exec is
SYNCHRONOUS and Context.sample is a COROUTINE. Whether one can reach the other is a fact about
two libraries, not about my code, and a source assertion cannot execute.

IT CANNOT. fastmcp's FunctionTool.run calls a sync tool DIRECTLY -- validate_python, then
`if inspect.isawaitable(result)`, which a sync result never is -- with no to_thread and no
executor anywhere in the package. A probe printed thread=MainThread from inside a running
tool. So a sync tool runs ON the event loop, and an outbound MCP request from it deadlocks:
the loop cannot run the coroutine, because the tool is what the loop is running. The first
bridge raised NoEventLoopError, the second timed out after twenty seconds, and both were
swallowed into REQUIRE_HUMAN -- indistinguishable, in shadow, from a client that simply has no
model.

These tests now assert the measured shape: an async tool reaches the client, a sync tool
refuses immediately and says why. If fastmcp ever threads sync tools, the sync test fails and
tells us the constraint has lifted.

In-memory transport: no network, no port, no browser, no Copilot. It proves the plumbing, not
that the production client can do any of this -- that client declares its own capabilities and
this file cannot speak for it.
"""
import json

import pytest

from fastmcp import Client, Context, FastMCP

from tools import judge_backend as B

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _server(fn):
    """A server whose one tool is SYNC, like shell_exec, and takes no Context parameter --
    also like shell_exec, which is why judge_backend reaches for get_context()."""
    mcp = FastMCP("judge-roundtrip-test")
    # THE SAME INSTALL THE DEPLOYMENT DOES. Without it the sync tool has no way back to the
    # event loop, and a test server that skips it is not testing the deployment.
    assert B.install(mcp), "the loop-capture middleware could not be installed"
    mcp.tool()(fn)
    return mcp


async def test_a_sync_tool_cannot_reach_the_client_and_says_so_at_once():
    """THE CONSTRAINT, PINNED. Not "it failed" -- it must fail in milliseconds with a reason,
    because a twenty-second timeout in front of every judged command would not be a subtle
    defect, it would make the layer unusable while looking configured."""
    out = {}

    def judged_probe(command: str) -> str:
        import time
        t0 = time.time()
        try:
            B.sampling_judge(json.dumps({"pending_command": command}))
            out["err"] = None
        except B.JudgeTransportError as exc:
            out["err"] = str(exc)
        out["elapsed"] = time.time() - t0
        out["on_loop"] = B.on_the_event_loop_thread()
        return "done"

    async def handler(messages, params, ctx):
        return '{"decision":"ALLOW"}'

    async with Client(_server(judged_probe), sampling_handler=handler) as client:
        await client.call_tool("judged_probe", {"command": "rm -rf /"})

    assert out["on_loop"] is True, "fastmcp now threads sync tools; revisit this whole file"
    assert out["err"] is not None, "a sync tool must not appear to reach the client"
    assert "async" in out["err"], "the refusal must say what would fix it: %r" % out["err"]
    assert out["elapsed"] < 2.0, "refused in %.1fs; it must not wait out a timeout" % out["elapsed"]


async def test_an_async_tool_does_reach_the_clients_model():
    """THE BRIDGE THAT WORKS, exercised end to end. This is the shape a judged tool has to
    take."""
    def _noop():
        pass

    async def judged_probe(command: str) -> str:
        return await B.sampling_judge_async(json.dumps({"pending_command": command}))

    async def handler(messages, params, ctx):
        return '{"decision":"BLOCK_AND_RETRY","categories":["destructive"],"reason":"deletes"}'

    async with Client(_server(judged_probe), sampling_handler=handler) as client:
        res = await client.call_tool("judged_probe", {"command": "rm -rf /"})

    text = "".join(getattr(c, "text", "") for c in res.content)
    assert "BLOCK_AND_RETRY" in text, "the model's answer did not come back: %r" % text


async def test_the_capability_check_sees_a_client_that_can_sample():
    """The negotiated capability, read from inside a live request rather than assumed."""
    reach = {}

    def probe() -> str:
        reach.update(B.availability())
        return "ok"

    async def handler(messages, params, ctx):
        return "{}"

    async with Client(_server(probe), sampling_handler=handler) as client:
        await client.call_tool("probe", {})

    assert reach["in_request"] is True
    assert reach["client_sampling"] is True


async def test_a_client_with_no_sampling_handler_is_reported_as_no_judge():
    """The other half, and the one that must not read as "allowed"."""
    out = {}

    def probe() -> str:
        out.update(B.availability())
        try:
            B.sampling_judge("{}")
            out["raised"] = False
        except B.JudgeTransportError:
            out["raised"] = True
        return "ok"

    async with Client(_server(probe)) as client:
        await client.call_tool("probe", {})

    assert out["in_request"] is True
    assert out["client_sampling"] is False
    assert out["raised"] is True, "a client that cannot sample must not silently succeed"


async def test_the_person_can_be_asked_and_their_approval_is_carried_back():
    """Elicitation over the same bridge. An acceptance is the only approval."""
    asked = {}

    async def probe(question: str) -> str:
        return repr(await B.ask_human_async(question))

    async def elicit_handler(message, response_type, params, ctx):
        asked["message"] = message
        from fastmcp.client.elicitation import ElicitResult
        return ElicitResult(action="accept", content=None)

    async with Client(_server(probe), elicitation_handler=elicit_handler) as client:
        res = await client.call_tool("probe", {"question": "may I delete build/?"})

    text = "".join(getattr(c, "text", "") for c in res.content)
    assert asked["message"] == "may I delete build/?"
    assert text == "True"


async def test_a_declining_person_is_not_an_approval():
    async def probe(question: str) -> str:
        return repr(await B.ask_human_async(question))

    async def elicit_handler(message, response_type, params, ctx):
        from fastmcp.client.elicitation import ElicitResult
        return ElicitResult(action="decline", content=None)

    async with Client(_server(probe), elicitation_handler=elicit_handler) as client:
        res = await client.call_tool("probe", {"question": "may I?"})

    text = "".join(getattr(c, "text", "") for c in res.content)
    assert text in ("False", "None"), "a decline must never read as True; got %r" % text


async def test_a_client_with_no_elicitation_cannot_approve_anything():
    async def probe(question: str) -> str:
        return "%r %r" % (B.human_available(), await B.ask_human_async(question))

    async with Client(_server(probe)) as client:
        res = await client.call_tool("probe", {"question": "may I?"})

    text = "".join(getattr(c, "text", "") for c in res.content)
    assert text.startswith("False"), "human_available must be False; got %r" % text
    assert "True" not in text, "no approval may come from a client that cannot ask"
