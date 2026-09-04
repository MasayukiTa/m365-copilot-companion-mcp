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

# Ordered by measured calls, descending, from .fleet/tool_events.jsonl at 11,391 calls
# (2026-09-04). Covers 96.4% of calls and 97.2% of argument-name failures. Membership is
# recomputed by tools/test_tool_catalogue.py when the ledger is present; that test skips in
# CI, where the ledger is not committed.
HOT = (
    "grep", "read_file", "run_python", "shell_exec", "list_directory", "replace_in_file",
    "find_files", "unlock", "web_search", "skill_match", "multi_edit", "write_file",
    "restore_point", "github_file", "web_fetch", "git_diff", "git_status", "job_wait",
    "git_log", "verify_file_contains",
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
