# -*- coding: utf-8 -*-
"""The catalogue is the first thing every agent reads, and it withheld the one fact they
needed.

RULE 1 orders every agent to call ``call_tool(name="")`` before answering anything about what
it can do. What comes back is 173 rows of ``name -- one-line summary``, in alphabetical order,
with no parameter names anywhere. So the caller invents them. Measured over the tool ledger
(11,391 calls):

    1,063 calls died on a guessed argument name.
    The 18 most-used tools hold 92.9% of those failures.

The per-tool rates say this is not carelessness. ``git_log`` is called 43 times and fails 27
of them -- 62.8%. ``git_status`` fails 57.5%, ``github_file`` 52.5%, ``verify_file_contains``
51.4%, ``find_files`` 39.7%, ``skill_match`` 81.5%. On those tools the caller is wrong more
often than right, which is a property of the catalogue rather than of the caller: the names
are not guessable and were never shown.

So show them, for the tools that are actually used. Twenty names cover 96.4% of all calls and
97.2% of all argument failures, and their signatures cost about 1,589 characters -- roughly
470 tokens against a catalogue that already spends 4,100. Everything else keeps exactly the
row it had.

TWO ORDERINGS, ON PURPOSE. The head is ranked by measured use, because that is a claim about
what the next task probably needs. The tail stays alphabetical, because nothing is known about
it and an agent looking for "pptx" should be able to find "pptx" -- ranking 115 never-called
tools by a count of zero would only scramble them.

NOTHING IS REMOVED. 115 of the 173 tools have never been called once, but the ledger is
almost entirely SWE-bench coding runs, so a database or Office tool was never *needed* rather
than found wanting. "Never called" and "not useful" are the same number here and different
facts, and dropping a capability on that evidence would be unrecoverable from inside the
system: the tool would stop being listed, therefore never be called, therefore stay dropped.
"""
import inspect

# Ordered by measured calls, descending, from .fleet/tool_events.jsonl at 31,447 calls
# (2026-09-10; previously derived at 11,391 calls on 2026-09-04, and drifted below its own
# thresholds as usage moved -- the head had fallen to 86.8% coverage and Kendall tau 0.69).
# Covers 91.6% of calls and 98.8% of argument-name failures.
#
# THE CEILING IS 93.5%, NOT 100%. call_tool.catalogue / .unknown / .signature are gateway
# pseudo-entries, not tools: they have no signature to show, so they cannot be in a head whose
# whole purpose is "parameter names given, so these can be called without a lookup" -- yet they
# are 2,057 of the 31,447 counted calls and therefore sit in the denominator. A future
# re-derivation that chases 96% by admitting them would be reporting a number rather than
# improving the head.
#
# Membership is recomputed by tools/test_tool_catalogue.py when the ledger is present; that
# test skips in CI, where the ledger is not committed.
HOT = (
    "read_file", "grep", "run_python", "list_directory", "shell_exec", "find_files",
    "unlock", "replace_in_file", "job_wait", "skill_match", "write_file", "git_status",
    "process_info", "web_search", "git_log", "file_metadata", "multi_edit", "edit_and_verify",
    "restore_point", "read_json", "list_my_tools", "job_status", "run_in_background", "git_diff",
    "github_file", "git_branch", "web_fetch", "glob", "verify_file_contains", "job_output",
)

_HEAD_NOTE = ("MOST USED -- parameter names given, so these can be called without a lookup:")
_TAIL_NOTE = ("EVERYTHING ELSE -- call_tool(name='X') first for X's parameters; guessing them "
              "is the single largest source of failed calls:")


def compact_signature(fn) -> str:
    """The parameter names and their defaults, without type annotations.

    The measured failure is a NAME failure -- ``query=`` for ``text=``, ``file=`` for
    ``path=`` -- so the annotations are the half that can be dropped. Keeping them would
    roughly double the cost of the head for no part of the signal. Defaults stay, because
    "path defaults to '.'" is the difference between a call and a lookup.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return "(...)"          # builtins and C functions have no inspectable signature
    out = []
    for p in sig.parameters.values():
        if p.kind is p.VAR_KEYWORD:
            out.append("**" + p.name)
        elif p.kind is p.VAR_POSITIONAL:
            out.append("*" + p.name)
        elif p.default is p.empty:
            out.append(p.name)
        else:
            out.append("%s=%r" % (p.name, p.default))
    return "(%s)" % ", ".join(out)


def _summary(fn) -> str:
    doc = (getattr(fn, "__doc__", "") or "").strip().splitlines()
    return doc[0].strip() if doc else ""


# ---------------------------------------------------------------------------------------
# CATEGORIES, AND WHY THE FLAT LIST HAD TO STOP BEING THE ANSWER
#
# The flat catalogue is 16,594 characters -- about 6,600 tokens -- and RULE 1 orders every
# agent to fetch it before doing anything. Measured over six hours of the tool ledger, 1,716
# calls:
#
#     call_tool.catalogue   112  (6.5%)   = 1,858,528 characters pushed into conversations
#     call_tool.unknown      76  (4.4%)   the agent guessed a name and missed
#     call_tool.signature    78  (4.5%)   it went back for the signature anyway
#
# 266 calls, 15.4% of everything, spent working out what to call. And one single turn
# contained seventeen `call_tool.unknown` in a row: the agent had read the catalogue and
# still could not find the name. So the flat list is not merely expensive, it is failing at
# the one job it has. A smaller list that is still unfindable would trade one failure for
# another.
#
# THE RULES ARE ORDERED AND THE FIRST MATCH WINS, which is what makes "exactly one category"
# a property rather than a hope -- a test asserts every registered tool lands in one, so a
# tool added later cannot quietly fall out of the index.
#
# Derived from how main.py already groups its registration block by comment, not from a new
# taxonomy invented here: that block is a category scheme somebody already wrote down.
_CATEGORIES = (
    ("files", "read, write, search and move files and folders",
     ("read_file", "write_file", "append_file", "replace_in_file", "multi_edit",
      "list_directory", "create_directory", "glob", "grep", "find_files", "read_json",
      "write_json", "delete_path", "trash_path", "copy_", "move_", "file_", "path_",
      "zip_", "archive_", "restore_point", "roll_back", "edit_and_verify")),
    ("run", "run code and commands, and check what they did",
     ("run_", "shell", "pwsh", "verify_", "process_", "code_", "python")),
    ("data", "spreadsheets, databases and structured data",
     ("data_", "excel", "sqlite", "odbc", "sql_", "csv", "semantic_", "table")),
    ("office", "documents, decks and drawings",
     ("pptx", "docx", "pdf_", "read_pdf", "render_", "diagram_", "create_pptx",
      "create_docx", "convert_")),
    ("mail", "mail and calendar, through the local Outlook",
     ("outlook",)),
    ("screen", "look at this machine's screen and act on it",
     ("screen_", "screenshot", "clipboard_", "image_", "read_image", "ocr_")),
    ("web", "fetch and search the web",
     ("web_", "search_", "http")),
    ("fleet", "hand work to the worker fleet, and watch it",
     ("fleet_", "job_", "task_", "gate_", "stop_", "schedule_", "recurrent_", "todo_",
      "local_loop", "notify_")),
    ("memory", "what this machine remembers, and the approved procedures",
     ("memory_", "skill_", "procedural_", "agent_", "runlog_", "trajectory", "golden",
      "evidence_", "forge_", "auto_")),
    ("git", "version control, and this repository's own history",
     ("git_", "github_")),
    ("access", "unlock, and what this server will tell you about itself",
     ("unlock", "list_unlocked", "secret", "auth_", "env_", "describe", "list_my_tools",
      "count_failures", "failure_signal", "heartbeat", "claim_turn", "commit_turn",
      "abort_turn", "read_job_context")),
)

#: Anything the rules above do not claim. Named rather than hidden: a growing "other" is the
#: signal that the categories have stopped describing what is registered, and a reader can
#: see it grow.
_OTHER = ("other", "everything not claimed by a category above")


def categorise(name: str) -> str:
    """Which category a tool name belongs to. First matching rule wins."""
    low = name.lower()
    for key, _desc, marks in _CATEGORIES:
        for m in marks:
            if low.startswith(m) or ("_" + m) in low or low == m.rstrip("_"):
                return key
    return _OTHER[0]


def by_category(all_tools: dict) -> dict:
    """{category: [tool names]}, every tool in exactly one."""
    out = {}
    for n in sorted(all_tools):
        out.setdefault(categorise(n), []).append(n)
    return out


def render_index(all_tools: dict, hot=HOT) -> str:
    """The category index: what exists, and how to open one. Under 1,500 characters.

    Keeps the MOST USED block, because the ledger's finding is that round trips are the
    cost: twenty tools carrying their parameter names is what stops a call from needing a
    lookup at all, and dropping it to save characters would buy the saving in the currency
    the measurement says is expensive.
    """
    groups = by_category(all_tools)
    head = [n for n in hot if n in all_tools]

    lines = ["%d tools, in %d categories. call_tool(name='X', arguments={...}) runs X."
             % (len(all_tools), len(groups)),
             "call_tool(name='<category>') lists that category. "
             "call_tool(name='<tool>') shows one tool's parameters.",
             "", _HEAD_NOTE]
    for n in head:
        lines.append("  %s%s -- %s" % (n, compact_signature(all_tools[n]),
                                       _summary(all_tools[n])))
    lines += ["", "CATEGORIES:"]
    described = {k: d for k, d, _m in _CATEGORIES}
    described[_OTHER[0]] = _OTHER[1]
    for key in list(described):
        if key in groups:
            lines.append("  %-8s (%d)  %s" % (key, len(groups[key]), described[key]))
    return "\n".join(lines)


def render_category(all_tools: dict, category: str) -> str:
    """One category's tools with their summaries, or a refusal that names the categories."""
    groups = by_category(all_tools)
    names = groups.get(category)
    if not names:
        return ("[call_tool: no category '%s'. Categories: %s]"
                % (category, ", ".join(sorted(groups))))
    lines = ["%s -- %d tool(s). call_tool(name='<tool>') shows one tool's parameters."
             % (category, len(names)), ""]
    for n in names:
        lines.append("  %s -- %s" % (n, _summary(all_tools[n])))
    return "\n".join(lines)


def nearest(all_tools: dict, name: str, limit: int = 4):
    """Tool names close to one that does not exist.

    The ledger shows 76 missed names in six hours, and a reply of "no such tool" turns each
    into a wasted round trip. Matching on shared word parts rather than edit distance,
    because the misses are the shape of `screenshot` for `screen_look` -- a real word in the
    right place, not a typo.
    """
    low = name.lower()
    parts = set(p for p in low.replace("-", "_").split("_") if len(p) > 2)
    parts.add(low)
    scored = []
    for n in all_tools:
        nl = n.lower()
        hits = sum(1 for p in parts if p in nl)
        if hits:
            scored.append((hits, -len(nl), n))
    scored.sort(reverse=True)
    return [n for _h, _l, n in scored[:limit]]


def render(all_tools: dict, hot=HOT) -> str:
    """The catalogue text: a ranked head with signatures, then the rest alphabetically.

    `all_tools` is main.py's name -> function mapping. A name in `hot` that is not in it is
    skipped rather than raising -- a renamed tool must not take the catalogue down with it,
    and the drift test is what says so out loud.
    """
    head = [n for n in hot if n in all_tools]
    seen = set(head)
    tail = sorted(n for n in all_tools if n not in seen)

    lines = ["%d tools available. call_tool(name='X', arguments={...}) runs X." % len(all_tools),
             "", _HEAD_NOTE]
    for n in head:
        lines.append("  %s%s -- %s" % (n, compact_signature(all_tools[n]), _summary(all_tools[n])))
    lines += ["", _TAIL_NOTE]
    for n in tail:
        lines.append("  %s -- %s" % (n, _summary(all_tools[n])))
    return "\n".join(lines)
