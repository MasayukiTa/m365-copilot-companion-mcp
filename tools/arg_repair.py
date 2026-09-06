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

    # THE CALLER PASSED THE GATEWAY'S OWN ENVELOPE ONE LEVEL TOO DEEP.
    #
    # call_tool(name=..., arguments={...}) is the documented form, and a caller that forwards
    # its own parameters verbatim arrives here as {"name": "<tool>", "arguments": {...}} -- the
    # real arguments intact, one wrapper out. This is NOT a guess like the remap below: the keys
    # are the gateway's two parameter names, `name` repeats the tool already being dispatched,
    # and the payload is a dict. There is no second reading, so it is unwrapped for mutating
    # tools too, where a guess would never be allowed.
    #
    # It matters because the refusal is silent about having the answer in hand. Measured on
    # .fleet/tool_events.jsonl: unlock was called this way with the correct password inside the
    # envelope and refused -- and unlock is the call that every mutating tool is gated behind,
    # so one such refusal locks the caller out of the server for the rest of the conversation.
    # 20 calls arrived carrying 'arguments' and 79 carrying 'name'.
    #
    # Guarded on the tool not having parameters of those names, so a tool that genuinely takes
    # `name` or `arguments` is never unwrapped out from under itself.
    _envelope_name_agrees = ("name" not in args
                             or not name
                             or str(args["name"]) == str(name))
    if (args and not (names & {"name", "arguments"})
            and set(args) <= {"name", "arguments"}
            and isinstance(args.get("arguments", {}), dict)
            and _envelope_name_agrees):
        args = dict(args.get("arguments") or {})

    if var_kw:
        # The tool takes **kwargs, so nothing is unexpected and there is nothing to repair.
        return {"action": RUN, "arguments": args, "message": ""}

    unexpected = [k for k in args if k not in names]
    unfilled = [p for p in required if p not in args]
    if not unexpected:
        if not unfilled:
            return {"action": RUN, "arguments": args, "message": ""}
        # NOTHING UNEXPECTED IS NOT THE SAME AS CALLABLE. An empty {} has no wrong names in it,
        # so this returned RUN and the call reached fn(**{}) and died on a bare
        # "missing 1 required positional argument". 26 calls ended that way. The caller is told
        # what to supply instead, in the same shape as every other explanation here.
        return {
            "action": EXPLAIN,
            "arguments": args,
            "message": ("[call_tool %s: missing required %s. The accepted form is %s. "
                        "Call it again with %s -- nothing was run.]"
                        % (name or getattr(fn, "__name__", "tool"),
                           ", ".join("'%s'" % u for u in sorted(unfilled)),
                           signature_text(fn, name),
                           ", ".join(sorted(required)))),
        }

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
