from typing import Callable, Optional

from .trace_ops import wrap_for_trace

_REGISTERED: list[dict] = []


def register(fn: Callable) -> Callable:
    """Decorator that records a tool's name and first-line docstring for introspection.

    Also applies the optional tool-call tracer (operator-D-adjacent observability): when
    MCP_TRACE_TOOLCALLS is set, every invocation is logged so the relay/cockpit can show
    a Claude-Code-style trail of what the agent actually did. The wrapper preserves the
    function signature, so FastMCP's schema generation is unaffected; when the env flag
    is unset (default) wrap_for_trace returns fn unchanged -- zero behaviour change."""
    doc = (fn.__doc__ or "").strip()
    summary = doc.splitlines()[0] if doc else "(no description)"
    _REGISTERED.append({"name": fn.__name__, "summary": summary})
    return wrap_for_trace(fn)


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
