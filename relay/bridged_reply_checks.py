# -*- coding: utf-8 -*-
"""Reply checks for the CONNECTOR-LESS route, which replace two checks the route skips.

relay/tool_bridge.py lists what the fleet relaxes for a bridged worker (see
`worker_is_bridged`). Two of those were not merely premises that stop holding; each guarded
against something that still happens on this route, and nothing took their place:

  1. "A call was printed as text instead of made" (relay_fleet._tried_to_call_a_tool). Under
     the connector route a model that writes `<invoke ...>` into its prose has made no call.
     Under this route the model is TOLD to write its calls into the reply, so the old test
     (any `<invoke` or `call_tool(` in the reply) cannot be used as it was. What is true on
     this route is narrower and certain: the coordinator (relay/socket_tools.py) executes
     exactly the fenced blocks whose info string is `call_tool` and whose body is a JSON
     object. A call written in any other form (the info string `json`, a tool name as info
     string, four backticks, a reply cut off inside the block, the connector's `<invoke>`
     markup) is NEVER executed and NEVER reported: the model carries on believing it called.
     That is the case this module recognises.

  2. "The model says it has no call_tool" (mcp_ready.reply_says_no_tools). Under this route the
     gateway always exists, so that claim is always wrong. And the same family of error exists
     for a single tool: the worker says `<name>` is not in the index, when the gateway has it.
     Measured 2026-10-23 (main.py, "AUTO-REGISTERED TOOLS"): the claim was TRUE there, because
     three registered tools were missing from the index; after that fix the same sentence from
     a model is a claim to be checked, not believed. So a claimed absence is verified against
     the gateway's own answer for that name, and only a name the gateway has is corrected.

WHAT IS EXECUTED IS KNOWN, NOT GUESSED. `executed` below is the number of blocks the
coordinator ran in the ask that produced the reply (chathub.Conversation._tool_calls, reset at
the start of every ask). The weak shape (the words `call_tool(` in prose) only counts when that
number is 0: a final summary that mentions a call it really made is normal.

PURE FUNCTIONS. Nothing here opens a connection or touches the fleet; the one lookup is a
callable the caller passes in.
"""
from __future__ import annotations

import json
import re
import threading

from relay import socket_tools as ST

#: The connector-route markup a model emits when it means to call a tool. Same dialects as
#: relay_fleet._PSEUDO_CALL_RE (tags, namespaced or not), without the `call_tool(` spelling,
#: which is a separate, weaker shape here.
_MARKUP_RE = re.compile(r"<\s*/?\s*(?:[\w-]+:)?(?:invoke|parameter|function_calls)\b", re.I)
_FUNCTION_SYNTAX_RE = re.compile(r"\bcall_tool\s*\(", re.I)
#: An opening fence that names the gateway in any spelling or backtick count.
_CALL_FENCE_OPEN_RE = re.compile(r"^[ \t]*`{3,}[ \t]*call[_\-. ]?tool\b", re.I | re.M)

#: Info strings that are a tool-call label, once separators are removed and case folded.
_LABELS = ("calltool", "toolcall", "tooluse", "function", "functioncall")

SHAPE_MARKUP = "markup"
SHAPE_FUNCTION = "function_syntax"
SHAPE_FENCED_JSON = "fenced_json"
SHAPE_FENCE_UNREAD = "fence_not_read"

#: What the model is shown, in the repository's own wording (docs/connector_less_route.md).
FORMAT_EXAMPLE = (
    "```call_tool\n"
    "{\"name\": \"<ツール名>\", \"arguments\": {}}\n"
    "```")


def _norm_label(info: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(info or "").lower())


def call_attempt_shapes(reply, executed=None):
    """The forms in which `reply` shows a tool call that the coordinator did NOT execute.

    `reply` is what the worker said (the final answer of the ask; calls that were executed are
    already stripped from it). `executed` is how many blocks were executed in that ask, or None
    when that is not known. Returns a list of shape names, [] when the reply shows no such call.
    """
    text = str(reply or "")
    if not text:
        return []
    shapes = []
    if _MARKUP_RE.search(text):
        shapes.append(SHAPE_MARKUP)
    # Fenced blocks the coordinator ignores because of how they are written.
    for m in ST._FENCE.finditer(text):
        info, body = m.group(1), m.group(2)
        if info == "call_tool":
            continue                      # executed (or reported unreadable) by the coordinator
        try:
            obj = json.loads(body)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        labelled = _norm_label(info) in _LABELS
        shaped = isinstance(obj.get("name"), str) and "arguments" in obj
        named = "_" in info and isinstance(obj, dict)
        if labelled or shaped or named:
            shapes.append(SHAPE_FENCED_JSON)
            break
    # A call_tool fence that the reader did not take: four backticks, odd case, or a reply
    # that ends inside the block. Each opening the reader took is one call or one reported error.
    opened = len(_CALL_FENCE_OPEN_RE.findall(text))
    if opened:
        read = ST.read_reply(text)
        if opened > len(read["calls"]) + len(read["unreadable"]):
            shapes.append(SHAPE_FENCE_UNREAD)
    if executed == 0 and _FUNCTION_SYNTAX_RE.search(text):
        shapes.append(SHAPE_FUNCTION)
    return shapes


# --------------------------------------------------------------------------------- absence claims

#: Phrases a model uses to say something is absent. A sentence needs one of these AND a
#: tool-like name before a lookup is made, and the lookup decides: a false match here costs one
#: gateway call, never a correction.
_ABSENT = (
    "does not exist", "doesn't exist", "do not exist", "don't exist", "not exist",
    "is not available", "isn't available", "not available", "unavailable",
    "not in the index", "not in the list", "not in the catalog", "not listed", "not registered",
    "not found", "no such tool", "no tool named", "no tool called", "cannot find", "can't find",
    "could not find", "couldn't find", "does not appear",
    "存在しません", "存在しない", "存在せず", "ありません", "見当たりません", "見つかりません",
    "見つからない", "登録されていません", "利用できません", "使えません", "索引にない",
    "一覧にない", "含まれていません")

_QUOTED = re.compile(r"[`'\"「『]([A-Za-z0-9][A-Za-z0-9_\-]{2,63})[`'\"」』]")
_UNDERSCORED = re.compile(r"(?<![A-Za-z0-9_\-.\\/])([A-Za-z][A-Za-z0-9]*(?:[_\-][A-Za-z0-9]+)+)(?![A-Za-z0-9_\-.\\/])")
_SENTENCE_SPLIT = re.compile(r"[。\n!?！？]|\.(?:\s|$)")
#: Names that are not a tool whatever the sentence says.
_NOT_A_TOOL = frozenset({"call_tool", "name", "arguments", "json", "true", "false", "null", "none"})

MAX_CLAIMED_NAMES = 3


def says_no_call_tool(reply) -> bool:
    """The model says the gateway itself is missing. Always wrong on this route."""
    from relay import mcp_ready
    return bool(mcp_ready.reply_says_no_tools(reply))


def tools_claimed_missing(reply, limit=MAX_CLAIMED_NAMES):
    """Names in `reply` that appear in a sentence saying something does not exist / is not listed.

    Candidates only: whether a name really is absent is the gateway's to say (see `verify_names`).
    Order of appearance, no duplicates, at most `limit`.
    """
    out = []
    for sentence in _SENTENCE_SPLIT.split(str(reply or "")):
        low = sentence.lower()
        if not any(p in low for p in _ABSENT):
            continue
        names = _QUOTED.findall(sentence) + _UNDERSCORED.findall(sentence)
        for n in names:
            if n.lower() in _NOT_A_TOOL or n in out:
                continue
            out.append(n)
            if len(out) >= limit:
                return out
    return out


#: The gateway's answer for a name it does not have (main.py, call_tool).
UNKNOWN_MARK = "no tool or category named"


def exists_in_gateway(name, run_tool, timeout_s=8.0):
    """True / False / None (could not be determined) for `name`, from the gateway's own answer.

    `run_tool(tool, args) -> (ok, text)` is the worker's own (tool_bridge.make_run_tool). A call
    with no `arguments` only describes: it runs nothing. It is made on a helper thread and
    abandoned after `timeout_s`, because the caller is the fleet's decision step and the
    run_tool's own bound is the long one a real tool call needs.
    """
    box = {}

    def _go():
        try:
            box["r"] = run_tool("call_tool", {"name": str(name)})
        except Exception as exc:        # a lookup that fails proves nothing
            box["e"] = exc

    t = threading.Thread(target=_go, name="bridged-name-lookup", daemon=True)
    t.start()
    t.join(timeout_s)
    if "r" not in box:
        return None
    ok, text = box["r"]
    if UNKNOWN_MARK in str(text or ""):
        return False
    return True if ok else None


def verify_names(names, run_tool, timeout_s=8.0):
    """The subset of `names` the gateway has (a tool or a category), in order."""
    return [n for n in names if exists_in_gateway(n, run_tool, timeout_s) is True]


# --------------------------------------------------------------------------------- the diagnosis

KIND_UNLANDED = "unlanded"
KIND_NO_GATEWAY = "no_gateway"
KIND_DENIED_TOOL = "denied_tool"


class Fault:
    """One thing wrong with a bridged worker's reply: what, the evidence, and what to say."""

    def __init__(self, kind, detail, steer):
        self.kind, self.detail, self.steer = kind, detail, steer

    def __repr__(self):
        return "Fault(%s, %s)" % (self.kind, self.detail)


def steer_unlanded(shapes) -> str:
    return ("直前の返信に書かれたツール呼び出しは、実行されていません(形式が違うため、こちらは読み取れませんでした: %s)。"
            "ツールを呼ぶには、info string を call_tool にしたコードブロックを1つ書き、本文には JSON の引数だけを書いてください。"
            "他の形式(json、ツール名、バッククォート4個、<invoke> など)は実行されません。形式:\n%s\n"
            "結果は次のメッセージで返ります。結果を受け取る前に、完了したとも、結果の値も書かないでください。"
            % (", ".join(shapes), FORMAT_EXAMPLE))


def steer_no_gateway() -> str:
    return ("call_tool はあります。このクライアントが渡しているツールは call_tool 1つで、他のツールはすべてその中にあります。"
            "info string を call_tool にしたコードブロックで呼んでください。まず索引を見る場合:\n"
            "```call_tool\n{\"name\": \"\"}\n```\n"
            "(arguments を書かないと、説明だけが返り、実行はされません。)")


def steer_denied_tool(names) -> str:
    parts = []
    for n in names:
        parts.append("索引に %s があります。call_tool(name='%s') で使い方を確認して呼んでください。" % (n, n))
    return ("".join(parts) +
            "使い方の確認は、次のコードブロックを書くと返ります(arguments は書かない):\n"
            "```call_tool\n{\"name\": \"%s\"}\n```" % names[0])


def diagnose(reply, executed, run_tool=None, *, lookup_timeout_s=8.0):
    """The Fault in a bridged worker's reply, or None.

    Order: an unlanded call first (the strongest evidence, and the one that also makes any result
    the reply states unbacked), then a missing-gateway claim, then a claimed-missing tool that the
    gateway has. `run_tool` is needed only for the last; without it that check is skipped.
    """
    shapes = call_attempt_shapes(reply, executed)
    if shapes:
        return Fault(KIND_UNLANDED, ",".join(shapes), steer_unlanded(shapes))
    if says_no_call_tool(reply):
        return Fault(KIND_NO_GATEWAY, "claims call_tool is missing", steer_no_gateway())
    if run_tool is not None:
        claimed = tools_claimed_missing(reply)
        if claimed:
            have = verify_names(claimed, run_tool, lookup_timeout_s)
            if have:
                return Fault(KIND_DENIED_TOOL, ",".join(have), steer_denied_tool(have))
    return None
