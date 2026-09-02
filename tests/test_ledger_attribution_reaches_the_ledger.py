"""Attribution must reach the ledger without reaching the tool.

_task and _worker were read for the ledger and then handed to the tool by fn(**_args), so any
caller that supplied them got

    TypeError: <tool>() got an unexpected keyword argument '_task'

The attribution could not be used without breaking the call it was attributing -- which is why
worker and task are empty on every row of a 1,879-row ledger, and why the failures recorded
there could not be traced to the run that produced them.

Measured live against the running server, before and after: the same probe returned a
TypeError and then succeeded, with `task=fanout-probe worker=w9` recorded either way.
"""
import importlib
import io
import os
import re

import pytest


def _dispatch_source():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return io.open(os.path.join(root, "main.py"), encoding="utf-8").read()


def test_the_attribution_is_removed_from_the_arguments():
    # Source-level and stated as such: the dispatch wrapper is inside a live MCP server and
    # invoking it here would register tools and open sockets. What is pinned is that the keys
    # are POPPED, not read -- reading them was the whole defect.
    src = _dispatch_source()
    assert '_args.pop("_task"' in src, "_task is not removed from the tool's arguments"
    assert '_args.pop("_worker"' in src, "_worker is not removed from the tool's arguments"


def test_the_attribution_is_not_read_back_out_of_the_arguments():
    # The old form. If it comes back, the tool starts receiving the keys again.
    src = _dispatch_source()
    assert '_args.get("_task"' not in src, "reverted to reading _task instead of popping it"
    assert '_args.get("_worker"' not in src, "reverted to reading _worker instead of popping it"


def test_the_ledger_still_gets_the_attribution():
    src = _dispatch_source()
    assert re.search(r"record_call\(\s*name,\s*_args,\s*task=_task,\s*worker=_worker",
                     src.replace("\n", " ")), "the ledger no longer receives task/worker"


def test_a_tool_that_rejects_extra_keywords_is_the_normal_case():
    """Why this mattered at all: tool functions have fixed signatures, so passing the
    attribution through was not a harmless extra -- it was a guaranteed TypeError on every
    call that used it.

    Checked against a real tools module rather than the server: importing main registers every
    tool with FastMCP, which is a lot of machinery to stand up to ask a question about function
    signatures. The runtime evidence for the defect is the live probe, which returned
    `TypeError: unlock() got an unexpected keyword argument '_task'` before the fix and
    succeeded after it.
    """
    import inspect
    from tools import file_ops
    fns = [v for k, v in vars(file_ops).items()
           if callable(v) and not k.startswith("_") and getattr(v, "__module__", "") ==
           file_ops.__name__]
    assert fns, "no tool functions found to inspect"
    kwargs_takers = 0
    for fn in fns:
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            continue
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            kwargs_takers += 1
    assert kwargs_takers < len(fns), (
        "every function here accepts **kwargs, so an unexpected keyword would be harmless and "
        "this premise does not hold -- re-check it")
