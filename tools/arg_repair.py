# -*- coding: utf-8 -*-
"""Repair a tool call whose argument NAMES were guessed, instead of rejecting it.

WHY THIS EXISTS. Under MCP_TOOL_MAP most tools are reachable only through the call_tool
gateway, and the catalogue it returns carries one-line summaries with no signatures. An agent
that picks a tool therefore has to guess its parameter names, and a wrong guess raised a bare
TypeError naming the rejected key and nothing else.

MEASURED over .fleet/tool_events.jsonl (11,385 calls): 1,037 died this way -- 9.1% of
everything dispatched.

    skill_match   145 / 178   81%      read_file   271 / 2421   11%
    git_log        27 / 43    63%      grep        168 / 2472    7%
    git_status     50 / 87    57%
    github_file    64 / 122   52%
    find_files    130 / 343   38%

    guessed: query 157, limit 104, repo 92, name 79, end_line 73, substring 57, path 40

The damage is not spread evenly, and the shape of it is the point. read_file recovers 99% of
the time -- the agent needs the file, so it keeps trying. skill_match recovers 34%: it is a
preliminary step the server ORDERS as RULE 2, so when it fails the agent simply proceeds to
the real work. 96 of 145 failures were abandoned, and skill_match has succeeded 33 times in
the entire ledger. The skill store was not empty and the matcher was not weak; the call never
arrived.

WHAT THIS DOES NOT DO. It does not guess. A remap happens only when the mapping is forced --
one unexpected name, one unfilled required parameter -- so there is exactly one thing the
caller could have meant. Anything less certain is explained back, not executed.

READ-ONLY ONLY, and this is the guard that matters. Silently redirecting an argument on a tool
that writes, deletes or executes could act on the wrong target while looking like a success.
A mutating tool gets the same explanation a human would: here is the accepted form, call it
again. The hint is derived mechanically in tools/tool_annotations.py.
"""
from __future__ import annotations

import inspect

#: The three answers this module gives.
RUN = "run"                 # nothing was wrong; call as supplied
REMAPPED = "remapped"       # one forced correction; safe to call with `arguments`
EXPLAIN = "explain"         # do not call; hand `message` back to the caller


def accepted(fn):
    """(all parameter names, required names, whether it takes **kwargs)."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return set(), set(), True          # unknowable: treat as accepting anything
    names, required, var_kw = set(), set(), False
    for p in sig.parameters.values():
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            var_kw = True
            continue
        if p.kind is inspect.Parameter.VAR_POSITIONAL:
            continue
        names.add(p.name)
        if p.default is inspect.Parameter.empty:
            required.add(p.name)
    return names, required, var_kw


def signature_text(fn, name=""):
    try:
        return "%s%s" % (name or getattr(fn, "__name__", "tool"), inspect.signature(fn))
    except (TypeError, ValueError):
        return "%s(...)" % (name or getattr(fn, "__name__", "tool"))


def repair(fn, arguments, name="", read_only=False):
    """Decide what to do with `arguments` before calling `fn`.

    Returns {"action", "arguments", "message"}. `message` is empty unless there is something
    the caller has to be told: either a note about a correction that was made, or the
    explanation that replaces the call.
    """
    args = dict(arguments or {})
    names, required, var_kw = accepted(fn)
    if var_kw:
        # The tool takes **kwargs, so nothing is unexpected and there is nothing to repair.
        return {"action": RUN, "arguments": args, "message": ""}

    unexpected = [k for k in args if k not in names]
    if not unexpected:
        return {"action": RUN, "arguments": args, "message": ""}

    unfilled = [p for p in required if p not in args]
    sig = signature_text(fn, name)

    # THE ONLY CASE THAT IS FORCED. One name the tool does not know, one required parameter
    # with nothing in it: there is no second reading. Anything else -- two stray names, a
    # stray name with every requirement already satisfied -- has more than one plausible
    # destination, and a wrong choice would run the tool on something the caller did not ask
    # for. Those are explained instead.
    if read_only and len(unexpected) == 1 and len(unfilled) == 1:
        wrong, right = unexpected[0], unfilled[0]
        fixed = dict(args)
        fixed[right] = fixed.pop(wrong)
        return {"action": REMAPPED, "arguments": fixed,
                "message": "note: '%s' was read as '%s'; the accepted form is %s"
                           % (wrong, right, sig)}

    why = "does not accept" if len(unexpected) == 1 else "does not accept any of"
    return {
        "action": EXPLAIN,
        "arguments": args,
        "message": ("[call_tool %s: %s %s. The accepted form is %s. "
                    "Required: %s. Call it again with those names -- nothing was run.]"
                    % (name or getattr(fn, "__name__", "tool"), why,
                       ", ".join("'%s'" % u for u in sorted(unexpected)), sig,
                       ", ".join(sorted(required)) or "(none)")),
    }
