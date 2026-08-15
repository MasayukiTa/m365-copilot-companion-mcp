import asyncio
import functools
import inspect
from typing import Callable, Optional

import anyio
import anyio.to_thread

from .trace_ops import wrap_for_trace

_REGISTERED: list[dict] = []


def _to_async(fn: Callable) -> Callable:
    """Wrap a synchronous tool function so FastMCP runs it OFF the event loop.

    WHY: FastMCP (uvicorn worker=1) drives a single asyncio event loop. Its
    FunctionTool.run calls the registered callable inline; if that callable is a
    plain `def` it executes ON the loop thread and BLOCKS it for the whole call.
    A single run_python (subprocess, up to 60s), shell_exec, pwsh_exec or odbc_query
    therefore freezes EVERY other in-flight request -> timeouts -> client disconnects
    -> CLOSE_WAIT pile-up -> FD exhaustion -> the server stops accepting. This wrapper
    turns each sync tool into a coroutine that offloads the blocking body to anyio's
    worker-thread pool, so the loop stays free to service other requests concurrently.

    COMPATIBILITY: FastMCP builds each tool's argument schema from the callable's
    signature + annotations (get_cached_typeadapter / inspect.signature). The wrapper
    therefore copies __name__, __doc__, __annotations__, __wrapped__ (via
    functools.wraps) AND sets __signature__ explicitly so inspect.signature(wrapper)
    is byte-for-byte identical to fn's. Kwargs are bound with functools.partial because
    anyio.to_thread.run_sync only forwards positional args.

    Already-async callables are returned UNCHANGED (no double-wrapping): FastMCP awaits
    them itself and they do not block the loop.
    """
    if inspect.iscoroutinefunction(fn):
        return fn

    @functools.wraps(fn)
    async def async_wrapper(*args, **kwargs):
        # Bind kwargs via partial; anyio.to_thread.run_sync forwards positional only.
        call = functools.partial(fn, *args, **kwargs)
        # Default anyio thread limiter (40 tokens) lets many heavy tools run in
        # parallel; one run_python no longer makes a concurrent grep wait.
        return await anyio.to_thread.run_sync(call)

    # FastMCP / pydantic read the SIGNATURE + ANNOTATIONS to generate the input schema.
    # functools.wraps copies __wrapped__/__doc__/__name__/__annotations__, but we set
    # __signature__ explicitly so inspect.signature(async_wrapper) == inspect.signature(fn).
    try:
        async_wrapper.__signature__ = inspect.signature(fn)
    except (ValueError, TypeError):
        pass
    return async_wrapper


def register(fn: Callable) -> Callable:
    """Decorator that records a tool's name and first-line docstring for introspection.

    Two transparent wrappers are applied, innermost first:
      1. wrap_for_trace -- optional tool-call tracing (operator-D observability). It is a
         no-op that returns fn unchanged unless MCP_TRACE_TOOLCALLS is set.
      2. _to_async -- offloads the (synchronous) tool body to a worker thread so a slow
         tool never freezes the shared asyncio event loop. This is the structural fix for
         the event-loop-starvation outages: every tool here is a plain `def`, so without
         this one heavy call blocks all other requests.

    Both wrappers preserve the function signature, type hints, name and docstring, so
    FastMCP's schema generation is unaffected and every tool stays visible to Copilot."""
    doc = (fn.__doc__ or "").strip()
    summary = doc.splitlines()[0] if doc else "(no description)"
    _REGISTERED.append({"name": fn.__name__, "summary": summary})
    # trace wrapper first (innermost), then offload to a thread so the trace timing also
    # measures the real work and the whole thing runs off-loop.
    return _to_async(_wrap_for_evidence(wrap_for_trace(fn), fn))


def _wrap_for_evidence(wrapped: Callable, original: Callable) -> Callable:
    """Record DIRECTLY-CALLED tools in the evidence trace.

    The trace was instrumented at the `call_tool` gateway on the reasoning that the gateway
    is the only point that sees every dispatched call with its real name. That is true of
    calls that GO THROUGH the gateway -- but a top-level registered tool is invoked by the
    host without touching it, so a security claim could be certified by a trace holding one
    innocuous gateway call while the interesting work happened beside it, unrecorded.

    So the recording point is here as well, where every tool passes regardless of how it was
    reached. `call_tool` itself is skipped: the gateway records the inner tool under its real
    name, and recording the wrapper too would add an entry saying only "a call was made".
    """
    if getattr(original, "__name__", "") == "call_tool":
        return wrapped

    @functools.wraps(wrapped)
    def evidence_wrapper(*args, **kwargs):
        try:
            from tools import evidence_trace as _trace
        except Exception:
            return wrapped(*args, **kwargs)
        if not _trace.enabled():
            return wrapped(*args, **kwargs)
        payload = dict(kwargs)
        if args:
            payload["_positional"] = list(args)
        try:
            out = wrapped(*args, **kwargs)
        except Exception as exc:
            _trace.record(original.__name__, payload, False,
                          "%s: %s" % (type(exc).__name__, exc), original)
            raise
        _trace.record(original.__name__, payload, True, out, original)
        return out

    try:
        evidence_wrapper.__signature__ = inspect.signature(wrapped)
    except (ValueError, TypeError):
        pass
    return evidence_wrapper


def list_my_tools(filter: Optional[str] = None) -> str:
    """List every tool exposed by this MCP server, with a one-line summary.

    Args:
        filter: Optional substring; only tool names containing it are returned.
    """
    items = _REGISTERED
    if filter:
        needle = filter.lower()
        items = [t for t in items if needle in t["name"].lower()]
    if not items:
        return "(no tools matched)"
    width = max(len(t["name"]) for t in items)
    lines = [f"{t['name']:<{width}}  {t['summary']}" for t in items]
    lines.append(f"--- {len(items)} tool(s)")
    return "\n".join(lines)
