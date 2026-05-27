from typing import Callable, Optional

_REGISTERED: list[dict] = []


def register(fn: Callable) -> Callable:
    """Decorator that records a tool's name and first-line docstring for introspection."""
    doc = (fn.__doc__ or "").strip()
    summary = doc.splitlines()[0] if doc else "(no description)"
    _REGISTERED.append({"name": fn.__name__, "summary": summary})
    return fn


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
