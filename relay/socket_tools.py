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


#: The batching instruction. Each response costs about 10 s whatever it contains (measured
#: 2026-10-19), so the number of responses is the lever. The English sentence is the one that was
#: measured (dependent goals 5.0 -> 3.3 responses, n=3-4: small, not conclusive); the Japanese
#: states the same rule and names the one exception, a call that needs an earlier call's result.
BATCH_RULE = (
    "互いの結果を必要としない呼び出しは、必ず1つの応答にすべてのブロックを並べて書くこと"
    "(1応答に1ブロックずつ出すと、往復の回数だけ時間が延びる)。"
    "前の呼び出しの結果が要るものだけ、次の応答にする。結果は書いた順に #番号 つきで返る。"
    " When several tool calls do not depend on each other, put all their fenced blocks in ONE reply. ")


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
        "本文に引数の JSON オブジェクトだけを書くこと。" + BATCH_RULE +
        "ツールが登録されているかを論じる必要はない -- 実在する。"
        "結果が返る前に完了したと述べないこと。\n\n"
        "<tools>\n" + block + "\n</tools>\n\n"
        "依頼:\n" + user_text)


def preflight_block(items) -> str:
    """The section of the FIRST prompt that carries what this side already looked up.

    `items` is [(label, ok, text)]: the index listing and the skill match, run here before the
    model's first turn. Measured 2026-10-19: in 20 of 23 first turns the model spent the whole
    turn asking for the index (and then skill_match), and a turn costs about 10 s. Only
    successful lookups are included; a failed one is left for the model to ask for itself.
    Returns "" when nothing succeeded, so a down server changes nothing.
    """
    parts = []
    for label, ok, text in items:
        if not ok:
            continue
        body = str(text or "")
        if len(body) > 8000:
            body = body[:8000] + "\n…(切り詰め: %d 文字)" % len(str(text))
        parts.append("## %s\n%s" % (label, body))
    if not parts:
        return ""
    return ("--- こちら側で実行済みの結果 ---\n"
            "上の指示の RULE 1 (call_tool(name='') で索引を見る) と RULE 2 (skill_match) は、"
            "この依頼についてこちらで済ませた。同じ呼び出しを繰り返さず、結果を踏まえて"
            "続きから始めること。\n\n" + "\n\n".join(parts) + "\n--- ここまで ---\n\n")


def _short(text, limit=80) -> str:
    """One line, bounded: a notice must not carry a model-sized payload back to the model."""
    one = " ".join(str(text).split())
    return one if len(one) <= limit else one[:limit] + "…"


def read_reply(reply: str, *, allowed=DEFAULT_ALLOWED, max_calls=MAX_CALLS_PER_REPLY):
    """Everything the reply asked for, and everything that could NOT be carried out.

    Returns {"calls": [(pos, name, args)], "unreadable": [(pos, reason)],
    "not_run": [pos, ...]}. `pos` is the 1-based position of the block among the reply's
    blocks whose info string is in `allowed`, so it is assigned here and the model's block
    does not have to carry an id. Blocks naming something else (ordinary code) are not
    counted, exactly as before.

    Nothing is dropped silently: a block that does not parse is `unreadable` with a reason,
    and a readable block past `max_calls` is `not_run` and is never executed.
    """
    calls, unreadable, not_run = [], [], []
    pos = 0
    for match in _FENCE.finditer(reply or ""):
        name, body = match.group(1), match.group(2)
        if name not in allowed:
            continue
        pos += 1
        try:
            args = json.loads(body)
        except Exception as exc:
            unreadable.append((pos, "JSON として読めない (%s)" % _short(exc, 60)))
            continue
        if not isinstance(args, dict):
            unreadable.append((pos, "JSON オブジェクトではない (%s)" % type(args).__name__))
            continue
        if len(calls) >= max_calls:
            not_run.append(pos)
            continue
        calls.append((pos, name, args))
    return {"calls": calls, "unreadable": unreadable, "not_run": not_run}


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


def result_turn(results, notices=None) -> str:
    """The next turn: what the tools returned, framed so it is not mistaken for a new request.

    Bounded per result. A tool that returns a megabyte would otherwise put a megabyte into the
    conversation, and cost here is charged per message rather than per byte -- but a message
    large enough to exhaust the model's budget ends the conversation regardless.
    """
    parts = []
    for item in results:
        name, ok, text = item[0], item[1], item[2]
        pos = item[3] if len(item) > 3 else None
        label = item[4] if len(item) > 4 else ""
        head = "%s → %s" % (name, "ok" if ok else "失敗")
        if pos is not None:
            head = "#%d %s" % (pos, head)
        if label:
            head += " [%s]" % label
        body = str(text or "")
        if len(body) > 8000:
            body = body[:8000] + "\n…(切り詰め: %d 文字)" % len(str(text))
        parts.append(head + "\n" + body)
    notes = [str(n) for n in (notices or ()) if n]
    tail = ("\n\n--- 注意 ---\n" + "\n".join(notes)) if notes else ""
    return ("ツールの実行結果は以下。これを踏まえて続けること。"
            "結果が不十分なら、必要なツールを追加で呼ぶこと。\n\n"
            "--- 実行結果 ---\n" + "\n\n".join(parts) + tail + "\n--- ここまで ---")


def notices_for(unreadable, not_run, max_calls=MAX_CALLS_PER_REPLY) -> list:
    """One short line per problem, so the model can resend exactly what did not happen.

    Numbers are the blocks' positions among the reply's call blocks, counted from the top.
    """
    notes = []
    for pos, reason in unreadable:
        notes.append("ブロック%d は読めなかったので実行していない: %s。直して再送すること。"
                     % (pos, reason))
    if not_run:
        notes.append("1回の応答で実行できるのは %d 件まで。ブロック%s の %d 件は"
                     "実行していない(実行されていない)。再送すること。"
                     % (max_calls, ",".join(str(p) for p in not_run), len(not_run)))
    return notes


def step(reply: str, run_tool, *, allowed=DEFAULT_ALLOWED, max_calls=MAX_CALLS_PER_REPLY):
    """One turn of the loop: read the reply, run what it asked for, return the next turn.

    Returns (next_turn_text, calls_made). `next_turn_text` is None when the model asked for
    nothing, which is how a turn ends.

    `run_tool(name, args) -> (ok, text)` is the caller's. A tool that raises is reported to the
    model as a failure rather than ending the turn: the model can often recover from a bad
    argument, and killing the goal over one is worse than telling it what happened.
    """
    read = read_reply(reply, allowed=allowed, max_calls=max_calls)
    notes = notices_for(read["unreadable"], read["not_run"], max_calls)
    if not read["calls"] and not notes:
        return None, []
    results = []
    for pos, name, args in read["calls"]:
        try:
            ok, text = run_tool(name, args)
        except Exception as exc:
            ok, text = False, "%s: %s" % (type(exc).__name__, exc)
        label = _short(args.get("name"), 40) if isinstance(args.get("name"), str) else ""
        results.append((name, bool(ok), text, pos, label))
    return result_turn(results, notes), [(n, a) for _p, n, a in read["calls"]]
