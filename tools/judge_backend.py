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


def _run_async(coro_fn, *args, **kwargs):
    """Run one coroutine from this synchronous tool.

    shell_exec is sync and fastmcp runs sync tools on a worker thread, so the host event loop
    is running on another thread and cannot simply be awaited. anyio's thread portal is the
    supported bridge back into it. If there is no portal -- the tool was called directly rather
    than through a request -- this raises, and the caller turns that into REQUIRE_HUMAN.
    """
    import anyio.from_thread
    return anyio.from_thread.run(lambda: coro_fn(*args, **kwargs))


def sampling_judge(request_json: str) -> str:
    """Ask the calling client's model one question, in a completion that has seen nothing else.

    Raises on any transport problem, which command_judge turns into REQUIRE_HUMAN rather than
    into an allow.
    """
    from tools.command_judge import SYSTEM_PROMPT

    ctx = _context()
    if ctx is None:
        raise JudgeTransportError("no MCP request context: nothing to ask")

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

def human_available() -> bool:
    """Whether there is anyone to ask. False outside a request, or on a client without
    elicitation -- and False is a refusal, not a pass."""
    return _context() is not None


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
