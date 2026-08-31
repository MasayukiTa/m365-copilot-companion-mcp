"""Where the judging question is actually asked, and nothing else.

SEPARATE FROM THE POLICY ON PURPOSE. tools/command_judge.py decides; this decides who to ask.
Keeping them apart is what lets the policy be tested exhaustively without a model, and what
lets a deployment with no judge available be a first-class state rather than an import error.

WITH NO BACKEND CONFIGURED THIS RETURNS None, and command_judge turns that into REQUIRE_HUMAN.
In shadow mode that is recorded; in enforce mode it is a refusal. That is the intended shape:
"no judge and no human" must mean "no execution", so a deployment that switches enforcement on
without a working judge finds out immediately rather than running unjudged.

WHY THE OBVIOUS BACKEND IS NOT HERE, and the first version of this file was wrong twice.

It asked the Copilot agent over relay/chathub.py, through `chathub.default_connect` and
`profile_token.supplier()`. NEITHER NAME EXISTS. I wrote them without opening either module,
which is the third time in one day that invented API names reached a file. Checked
2026-08-31: chathub exposes `Conversation(token_supplier, *, template=...)` and
`Conversation.ask(text, *, connect=...)`, the connect function lives in relay/socket_route.py
as `websocket_connect`, and tokens are captured from the signed-in browser by
relay/profile_token.py's `capture_via_profile` / `capture_fn`.

The second error survives fixing the names. THE PROCESS BOUNDARY IS THE REAL OBSTACLE. A
socket conversation needs a captured request template and a live token for a specific agent
URL, both held by the SocketRoute inside the FLEET process. shell_exec runs inside the MCP
SERVER process, which has neither and cannot get either -- chathub's own docstring says a
template's absence means "no socket route", not "try anyway". A judge wired that way would
have worked in whichever process happened to have a route and silently degraded everywhere
else, which is the failure shape this repository keeps paying for: a gate that is present,
looks configured, and is not running.

WHAT THE SERVER ACTUALLY HAS is the MCP client that called it. `sampling` asks that client to
run one completion; `elicit` asks the human at that client a question. Both are in the protocol
and in the installed fastmcp (2.14.7, verified), both need no install, no sandbox, no
administrator, and no second machine -- which are the constraints this deployment is under.
Whether a given client implements them is a property of that client and is not knowable from
here, so an unsupported client must read as "no judge" and never as "allowed".
"""
from __future__ import annotations

import os
from typing import Callable, Optional

BACKEND_ENV = "MCP_JUDGE_BACKEND"

#: How long to wait for a judging answer. A judge sits in front of every non-exempt command, so
#: this is latency the user feels on ordinary work; and a judge that has not answered in this
#: long is not going to make the command safer by answering later.
TIMEOUT_ENV = "MCP_JUDGE_TIMEOUT_S"
DEFAULT_TIMEOUT_S = 20.0


class JudgeTransportError(RuntimeError):
    """The question could not be asked. Distinct from an answer that could not be understood
    (command_judge.JudgeUnavailable), though both end at REQUIRE_HUMAN -- the distinction is
    for the audit line, where "nobody was reachable" and "the answer was gibberish" are
    different problems with different fixes."""


def timeout_s() -> float:
    try:
        return float(os.environ.get(TIMEOUT_ENV) or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S


def get() -> Optional[Callable[[str], str]]:
    """The judge callable for this deployment, or None.

        MCP_JUDGE_BACKEND=sampling  ask the calling MCP client to run one completion
        MCP_JUDGE_BACKEND=none      no judge (default)

    The separation that matters is not a different model -- it is a separate call, a fixed
    system prompt, input assembled by this server rather than by the caller being judged, and
    no tools offered to the judge. `sampling` gives all four: the request is built here, the
    instructions go in the `system_prompt` field rather than being concatenated into the text,
    and no tools are passed.
    """
    name = (os.environ.get(BACKEND_ENV) or "none").strip().lower()
    if name in ("", "none", "off"):
        return None
    if name == "sampling":
        return sampling_judge
    # An unrecognised name is not a licence to run unjudged, but it is also not something this
    # function can fix. None -> REQUIRE_HUMAN, which is the safe reading of "you asked for a
    # judge I do not have".
    return None


def _context():
    """The current request's MCP context, or None outside a request.

    fastmcp injects a Context into tools that declare one; shell_exec does not, and adding a
    required parameter would change the signature every direct caller and test already uses.
    `get_context()` is the accessor for exactly this case.
    """
    try:
        from fastmcp.server.dependencies import get_context
    except Exception:
        return None
    try:
        return get_context()
    except Exception:
        # Raises when there is no active request -- a direct call, a test, a script.
        return None


#: The event loop serving MCP requests, captured on the async side. See _run_async.
_LOOP = None


def remember_event_loop(loop=None) -> bool:
    """Record the loop that serves requests. Called from the async side, once per call is fine.

    THE SYNC TOOL CANNOT FIND THIS FOR ITSELF, which is the whole reason it is here.
    """
    global _LOOP
    if loop is None:
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
    _LOOP = loop
    return True


def loop_available() -> bool:
    return _LOOP is not None and _LOOP.is_running()


def install(mcp) -> bool:
    """Teach a FastMCP server to hand this module its event loop.

    Must be called on any server whose tools will judge, INCLUDING IN TESTS -- a test server
    that skips it is not testing the deployment. `availability()` reports whether it happened,
    so a deployment that forgets shows up in the judge log rather than silently having no
    judge.
    """
    try:
        from fastmcp.server.middleware import Middleware

        class _CaptureLoop(Middleware):
            async def on_call_tool(self, context, call_next):
                remember_event_loop()
                return await call_next(context)

        mcp.add_middleware(_CaptureLoop())
        return True
    except Exception:
        return False


def on_the_event_loop_thread() -> bool:
    """True when this synchronous code is running ON the loop, not beside it.

    FALSE FOR THIS SERVER'S TOOLS, and the check is here because I first concluded otherwise.

    fastmcp's FunctionTool.run does call a sync function directly, on the loop thread -- there
    is no to_thread and no executor in the package. From that I concluded a sync tool can never
    make an outbound MCP request, and committed it. It was wrong: main.py registers
    `register(tool)`, and tools/registry.py's register() already wraps every tool in
    `anyio.to_thread.run_sync`, precisely so a slow tool cannot freeze the loop. Under that
    wrapper the tool runs in an anyio worker thread and the round trip works -- measured,
    `thread=AnyIO worker thread`, verdict returned.

    What was actually wrong was my test: it registered a bare `mcp.tool()(fn)`, a shape the
    deployment does not have, and so measured a configuration that does not exist.

    The check stays because the failure it names is real when it happens -- a tool registered
    without register(), which is exactly what that test did -- and because the alternative is
    a twenty-second deadlock reported as a timeout.
    """
    import asyncio
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _run_async(coro_fn, *args, **kwargs):
    """Run one coroutine from synchronous code, or say plainly that it cannot be done here.

    THE NORMAL PATH IS THE FIRST ONE. tools/registry.py's register() runs every tool through
    `anyio.to_thread.run_sync`, so a registered tool is in an anyio worker thread and
    `anyio.from_thread.run` has its token. That is measured, in
    tools/test_judge_live_roundtrip.py, through the same registration main.py uses.

    The refusal below is for a tool registered WITHOUT that wrapper, which runs on the loop
    thread. There, scheduling a coroutine and blocking for it deadlocks -- the loop cannot run
    what it is currently inside -- and the deadlock expires as a timeout, twenty seconds per
    judged command, reported as a transport fault. Failing in milliseconds with a reason is the
    difference between a bug report and a mystery.
    """
    if on_the_event_loop_thread():
        raise JudgeTransportError(
            "this tool runs on the server's event loop, so it cannot make an outbound MCP "
            "request; the judged tool has to be async for sampling or elicitation to reach "
            "the client")
    import anyio.from_thread
    try:
        return anyio.from_thread.run(lambda: coro_fn(*args, **kwargs))
    except Exception as exc:
        # Only an anyio-worker-thread problem falls through to the loop route; a failure from
        # the coroutine itself must not be retried, or one judging call becomes two.
        if type(exc).__name__ not in ("MissingTokenError", "NoEventLoopError", "RuntimeError"):
            raise
    import asyncio
    loop = _LOOP
    if loop is not None and loop.is_running():
        fut = asyncio.run_coroutine_threadsafe(coro_fn(*args, **kwargs), loop)
        # Bounded independently of the inner timeout, so a loop that stops answering cannot
        # park a tool call forever.
        return fut.result(timeout=timeout_s() + 5.0)
    raise JudgeTransportError("no way back to the event loop from this thread")


async def sampling_judge_async(request_json: str) -> str:
    """The same question, asked from the async side, where it actually works.

    This is the judge a tool gets once it is `async def`. Nothing about the policy differs --
    the request is still built by command_judge, the rules still travel in system_prompt, no
    tools are offered. The only difference is that there is a running loop to await on.
    """
    from tools.command_judge import SYSTEM_PROMPT
    import anyio

    ctx = _context()
    if ctx is None:
        raise JudgeTransportError("no MCP request context: nothing to ask")
    if not sampling_supported():
        raise JudgeTransportError(
            "the connected client did not declare the sampling capability, so there is no "
            "model to ask from inside this server")
    try:
        with anyio.fail_after(timeout_s()):
            result = await ctx.sample(request_json, system_prompt=SYSTEM_PROMPT,
                                      max_tokens=300, temperature=0.0)
    except Exception as exc:
        raise JudgeTransportError("%s: %s" % (type(exc).__name__, str(exc)[:160]))
    return _text_of(result)


async def ask_human_async(question: str) -> Optional[bool]:
    """The approval question, from the async side. Same three-valued answer as ask_human."""
    import anyio

    ctx = _context()
    if ctx is None:
        return None
    try:
        with anyio.fail_after(timeout_s()):
            result = await ctx.elicit(question, response_type=None)
    except Exception:
        return None
    name = type(result).__name__
    if name.startswith("Accepted"):
        return True
    if name.startswith("Declined") or name.startswith("Cancelled"):
        return False
    return None


def sampling_judge(request_json: str) -> str:
    """Ask the calling client's model one question, in a completion that has seen nothing else.

    Raises on any transport problem, which command_judge turns into REQUIRE_HUMAN rather than
    into an allow.
    """
    from tools.command_judge import SYSTEM_PROMPT

    ctx = _context()
    if ctx is None:
        raise JudgeTransportError("no MCP request context: nothing to ask")
    if not sampling_supported():
        # ASKED BEFORE TRYING. A client that never declared sampling will not answer, and
        # finding that out by waiting for the timeout costs every judged command the full
        # timeout and reports it as a transport fault rather than as a missing capability --
        # two different problems with two different fixes.
        raise JudgeTransportError(
            "the connected client did not declare the sampling capability, so there is no "
            "model to ask from inside this server")

    async def _ask():
        # THE INSTRUCTIONS AND THE PAYLOAD TRAVEL IN DIFFERENT FIELDS. The request is a JSON
        # object in the message; the rules are the system prompt. Text inside the command
        # therefore cannot close the instruction block and open a new one, which it could if
        # the two were concatenated -- as the first version of this file did.
        #
        # NO TOOLS ARE OFFERED. A judge that can act is not a judge.
        import anyio
        with anyio.fail_after(timeout_s()):
            return await ctx.sample(request_json, system_prompt=SYSTEM_PROMPT,
                                    max_tokens=300, temperature=0.0)

    try:
        result = _run_async(_ask)
    except Exception as exc:
        raise JudgeTransportError("%s: %s" % (type(exc).__name__, str(exc)[:160]))
    return _text_of(result)


def _text_of(result) -> str:
    """The answer's text, whatever shape the client's result object takes."""
    for attr in ("text", "content", "message"):
        val = getattr(result, attr, None)
        if isinstance(val, str) and val.strip():
            return val
        if val is not None and not isinstance(val, str):
            inner = getattr(val, "text", None)
            if isinstance(inner, str) and inner.strip():
                return inner
    if isinstance(result, str):
        return result
    return str(result or "")


# ── the human, who may overrule either layer ──────────────────────────────────────────────
#
# "引っかかったものでも問題なしとユーザが明示的に承認したら当然実行OK。それはユーザの責任" --
# the owner's requirement, and REQUIRE_HUMAN is worth nothing without somewhere to ask. MCP
# elicitation is that channel: the question reaches the person at the client, and their answer
# is an explicit approval rather than an inferred one.
#
# NOT IN SHADOW MODE. Shadow exists to measure without changing behaviour, and a prompt is a
# change in behaviour -- an interruption on every unrecognised command would be the fastest
# possible way to make people turn the layer off before it has been measured once.

def _client_supports(**kw) -> bool:
    """Did the CLIENT declare this capability when it connected?

    ASKED, NOT ASSUMED. The MCP handshake carries the client's capabilities, so whether there
    is a judge or a person to ask is knowable at connect time rather than discoverable one
    failed command at a time. Every uncertainty resolves to False: no context, no session, an
    older library without the check, an exception -- because this answer feeds
    `human_available`, where True means "REQUIRE_HUMAN does not block".

    A hand-written allowlist that resolves the unknown case to "supported" is the fail-open
    this repository has already recorded once, and it would be worse here than anywhere else:
    it would turn "ask a person" into "carry on" on precisely the deployments with no person.
    """
    ctx = _context()
    if ctx is None:
        return False
    try:
        from mcp.types import ClientCapabilities
        session = getattr(ctx, "session", None)
        if session is None:
            return False
        return bool(session.check_client_capability(ClientCapabilities(**kw)))
    except Exception:
        return False


def sampling_supported() -> bool:
    """Whether the calling client can run a completion for us."""
    try:
        from mcp.types import SamplingCapability
        return _client_supports(sampling=SamplingCapability())
    except Exception:
        return False


def elicitation_supported() -> bool:
    """Whether the calling client can put a question to its user."""
    try:
        from mcp.types import ElicitationCapability
        return _client_supports(elicitation=ElicitationCapability())
    except Exception:
        return False


def human_available() -> bool:
    """Whether there is anyone to ask.

    THE DECLARED CAPABILITY, NOT THE PRESENCE OF A REQUEST. The first version returned True
    whenever a context existed, which is not the same question: a client can call a tool and
    have no way to show its user anything. Wired into
    command_judge.outcome_blocks_execution, that overstatement turns REQUIRE_HUMAN from a
    refusal into an allow on exactly the clients where nobody can be asked.
    """
    return elicitation_supported()


def availability() -> dict:
    """What this layer can actually reach right now, for the audit line and the log.

    A BOOLEAN CANNOT HOLD THE STATES THAT MATTER: never configured, configured but the client
    cannot do it, and configured and reachable. The first two are both "no judge" in effect
    and need completely different fixes, and a log that flattens them tells nobody which.
    """
    name = (os.environ.get(BACKEND_ENV) or "none").strip().lower()
    return {
        "backend": name,
        "configured": name not in ("", "none", "off"),
        "in_request": _context() is not None,
        "client_sampling": sampling_supported(),
        "client_elicitation": elicitation_supported(),
        # False here means install() was never called on this server, and NOTHING can be
        # asked however capable the client is. Silent otherwise.
        "loop": loop_available(),
    }


def ask_human(question: str) -> Optional[bool]:
    """Put one approval question to the person at the client.

    True  -- they approved; the command runs on their responsibility, and the audit line says
             it was a human decision rather than a judged one.
    False -- they declined.
    None  -- nobody could be asked (no context, no elicitation support, timeout). NOT an
             approval: the caller must treat it exactly as a decline.
    """
    ctx = _context()
    if ctx is None:
        return None

    async def _ask():
        import anyio
        with anyio.fail_after(timeout_s()):
            return await ctx.elicit(question, response_type=None)

    try:
        result = _run_async(_ask)
    except Exception:
        return None
    # fastmcp returns AcceptedElicitation / DeclinedElicitation / CancelledElicitation. Only an
    # acceptance is an approval; anything else, including a shape this code does not recognise,
    # is not.
    name = type(result).__name__
    if name.startswith("Accepted"):
        return True
    if name.startswith("Declined") or name.startswith("Cancelled"):
        return False
    return None
