"""Tool calling over a plain socket, where the model asks and THIS side executes.

THE MECHANISM, AND WHY IT DISSOLVES THE AUTHENTICATION QUESTION

There are two ways to give Copilot tools. One declares an MCP server in the request's
`plugins` array and lets Copilot orchestrate it -- and that route has no field for
credentials, so it cannot reach an MCP server that requires a key, which ours does.

The other never tells Copilot the server exists. The tools are described in the prompt, the
model answers with a fenced block naming the tool it wants, and the caller runs it and feeds
the result back. Copilot writes text; nothing here connects outward on its behalf.

That difference removes three problems at once:

  * AUTH. This process calls its own MCP with its own key. There is no credential to hand to
    anybody, because nobody else is calling.
  * CONSENT. The consent card exists because Copilot is being asked to reach an external
    connector. It is not, so there is nothing to approve -- and the DOM machinery that
    detects and clicks those cards, which a socket has no equivalent for, stops being needed.
  * THE AGENT. The socket reaches the default Copilot, not a registered Copilot Studio agent
    -- verified: the reference implementation hardcodes `agent=web` and ships no way to name
    one. Under this mechanism that does not matter, because the tools do not come from the
    agent.

WHAT IT COSTS, STATED PLAINLY

Copilot stops being an agent that holds our tools and becomes a language model inside our
loop. Work IQ and tenant grounding are not available this way, so anything that needs company
mail, files or Teams stays on the tab. Coding work does not need them, and coding work is what
runs eight-wide -- which is where the memory goes.

ONE TOOL IS ADVERTISED, NOT ~160. The existing PROTOCOL already tells the model that every
tool lives behind a `call_tool` gateway and that it must list them before claiming anything.
Keeping that shape means the discipline the model already follows does not change; only the
transport does.

NOTHING HERE EXECUTES ANYTHING. `run_tool` is supplied by the caller. This module builds a
prompt, reads a reply, and says what was asked for.
"""
from __future__ import annotations

import json
import re

#: Only these may be invoked, whatever the model writes. A fenced block is model output, and
#: model output is untrusted input: without this, a reply that happens to contain a block
#: named for something else would be a request this side carried out.
DEFAULT_ALLOWED = ("call_tool",)

#: A model that keeps talking instead of calling is not making progress. Bounded so a turn
#: cannot loop forever on a model that will not commit.
MAX_CALLS_PER_REPLY = 8

_FENCE = re.compile(
    r"^[ \t]*```[ \t]*([A-Za-z_][A-Za-z0-9_.\-]*)[ \t]*\r?\n(.*?)\r?\n[ \t]*```",
    re.DOTALL | re.MULTILINE)


class ToolProtocolError(RuntimeError):
    """The reply could not be read as a tool call. Callers fall back rather than guess."""


def tools_block(catalogue) -> str:
    """The `<tools>` section describing what may be called.

    `catalogue` is [{"name":..., "description":..., "parameters": {...}}]. Kept to the gateway
    shape this system already uses rather than expanded into every tool, so the model's
    instructions do not change with the transport.
    """
    parts = []
    for tool in catalogue or []:
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        params = tool.get("parameters")
        params = json.dumps(params, ensure_ascii=False, sort_keys=True) if params else "{}"
        parts.append("%s — %s\n```%s\n%s\n```"
                     % (name, tool.get("description", ""), name, params))
    return "\n\n".join(parts)


def build_prompt(user_text: str, catalogue, *, protocol: str = "") -> str:
    """The turn to send: the existing protocol, the tool description, then the request.

    ORDER MATTERS AND IS DELIBERATE. The request goes last because a model that has just read
    a long block of instructions answers the thing nearest its own turn; putting the task
    first has it answer the instructions instead.
    """
    block = tools_block(catalogue)
    if not block:
        return (protocol or "") + user_text
    return (
        (protocol or "")
        + "呼べるツールは以下。使うときは、info string をツール名にしたコードブロックを出し、"
        "本文に引数の JSON オブジェクトだけを書くこと。独立した操作は複数ブロックを"
        "1つの応答にまとめてよい。ツールが登録されているかを論じる必要はない -- 実在する。"
        "結果が返る前に完了したと述べないこと。\n\n"
        "<tools>\n" + block + "\n</tools>\n\n"
        "依頼:\n" + user_text)


def parse_calls(reply: str, *, allowed=DEFAULT_ALLOWED, max_calls=MAX_CALLS_PER_REPLY):
    """The tool calls a reply is asking for, as [(name, args_dict)].

    A block whose body is not a JSON OBJECT is skipped rather than guessed at: the model
    writes ordinary fenced code too, and reading ```python as a tool call would execute
    whatever happened to parse. A block naming something outside `allowed` is skipped for the
    same reason at a higher stake.
    """
    out = []
    for match in _FENCE.finditer(reply or ""):
        name, body = match.group(1), match.group(2)
        if name not in allowed:
            continue
        try:
            args = json.loads(body)
        except Exception:
            continue
        if not isinstance(args, dict):
            continue
        out.append((name, args))
        if len(out) >= max_calls:
            break
    return out


def strip_calls(reply: str, *, allowed=DEFAULT_ALLOWED) -> str:
    """The reply with its TOOL-CALL blocks removed -- what the model actually said.

    ONLY the blocks that parse as calls. Removing every fenced block would delete the model's
    own code, which is a deliverable rather than a request -- on a coding goal that is the
    answer being thrown away, and the caller would never see that it existed.
    """
    def _drop(match):
        name, body = match.group(1), match.group(2)
        if name not in allowed:
            return match.group(0)
        try:
            args = json.loads(body)
        except Exception:
            return match.group(0)
        return "" if isinstance(args, dict) else match.group(0)

    return _FENCE.sub(_drop, reply or "").strip()


def result_turn(results) -> str:
    """The next turn: what the tools returned, framed so it is not mistaken for a new request.

    Bounded per result. A tool that returns a megabyte would otherwise put a megabyte into the
    conversation, and cost here is charged per message rather than per byte -- but a message
    large enough to exhaust the model's budget ends the conversation regardless.
    """
    parts = []
    for name, ok, text in results:
        head = "%s → %s" % (name, "ok" if ok else "失敗")
        body = str(text or "")
        if len(body) > 8000:
            body = body[:8000] + "\n…(切り詰め: %d 文字)" % len(str(text))
        parts.append(head + "\n" + body)
    return ("ツールの実行結果は以下。これを踏まえて続けること。"
            "結果が不十分なら、必要なツールを追加で呼ぶこと。\n\n"
            "--- 実行結果 ---\n" + "\n\n".join(parts) + "\n--- ここまで ---")


def step(reply: str, run_tool, *, allowed=DEFAULT_ALLOWED, max_calls=MAX_CALLS_PER_REPLY):
    """One turn of the loop: read the reply, run what it asked for, return the next turn.

    Returns (next_turn_text, calls_made). `next_turn_text` is None when the model asked for
    nothing, which is how a turn ends.

    `run_tool(name, args) -> (ok, text)` is the caller's. A tool that raises is reported to the
    model as a failure rather than ending the turn: the model can often recover from a bad
    argument, and killing the goal over one is worse than telling it what happened.
    """
    calls = parse_calls(reply, allowed=allowed, max_calls=max_calls)
    if not calls:
        return None, []
    results = []
    for name, args in calls:
        try:
            ok, text = run_tool(name, args)
        except Exception as exc:
            ok, text = False, "%s: %s" % (type(exc).__name__, exc)
        results.append((name, bool(ok), text))
    return result_turn(results), calls
