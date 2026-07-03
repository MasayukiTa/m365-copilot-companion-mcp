"""MCP Resource pilot (book: MCP spec 21.3.2 / Quick Reference 28.17).

The book prescribes modelling side-effect-free READ data as MCP **Resources**,
a primitive distinct from Tools. This repo currently registers all ~138
capabilities as Tools and uses no Resources. This module is a SMALL, ADDITIVE
pilot: a handful of genuinely read-only, non-gated data sources exposed via
`@mcp.resource(...)` in main.py, alongside (not instead of) the existing
tools. Nothing here removes or changes any tool's behavior.

Guardrails baked in on purpose:
  * Every function here is read-only. None calls require_unlocked() and none
    mutates state -- that would violate the definition of a Resource.
  * The file resource reuses tools.file_ops._validate_path VERBATIM (same
    function object the read_file TOOL uses), so it is exactly as permissive
    (or restrictive) as read_file and never widens access.
  * The server-info resource reports only counts/flags/names, never secret
    values (no API key, no unlock password, no raw IPs beyond a count).
"""
import os

from .file_ops import ALLOWED_BASES, _validate_path
from .security import _load_state


def server_info_resource() -> str:
    """companion://server/info -- read-only server capability/config summary.

    Reuses the same data sources as env_info()/list_unlocked() but reports
    ONLY counts, flags, and (for allowed bases) directory names -- never
    secret values such as the API key or unlock password, and never the
    raw unlocked-IP addresses themselves (count only).
    """
    from .registry import _REGISTERED

    tool_map_on = os.environ.get("MCP_TOOL_MAP") == "1"
    if ALLOWED_BASES is None:
        base_desc = "unrestricted (default-open; MCP_ALLOWED_BASE unset)"
    else:
        base_desc = ", ".join(str(b) for b in ALLOWED_BASES)

    try:
        unlocked_count = len(_load_state())
    except Exception:
        unlocked_count = -1  # unavailable, not a secret -- just a signal

    lines = [
        "server: m365-copilot-companion-mcp",
        f"tools_registered: {len(_REGISTERED)}",
        f"tool_map_gateway_mode: {tool_map_on}",
        f"allowed_base: {base_desc}",
        f"unlocked_ip_count: {unlocked_count}",
    ]
    return "\n".join(lines)


def file_resource(path: str) -> str:
    """companion://file/{path*} -- read a text file UNDER the allowed base.

    Templated resource mirroring the book's canonical get_note example. The
    URI template uses the RFC 6570 wildcard form {path*} (registered as
    "companion://file/{path*}" in main.py) because a plain {path} segment
    cannot contain "/" -- a real filesystem path needs multiple segments.

    Uses the EXACT SAME _validate_path guardrail as the read_file tool, so a
    request for a path outside the allowed base is rejected identically (a
    PermissionError, surfaced here as a "[file_resource error: ...]" string
    rather than tool JSON, since resource reads don't go through the tool
    error-formatting path). No side effects: this never writes.
    """
    try:
        p = _validate_path(path)
        if not p.is_file():
            return f"[file_resource error: not a file: {p}]"
        return p.read_text(encoding="utf-8")
    except Exception as e:
        return f"[file_resource error: {type(e).__name__}: {e}]"


def jobs_resource() -> str:
    """companion://jobs/list -- current background jobs (read-only snapshot).

    Delegates to tools.jobs.job_list(), which only reads the in-process job
    table (no filesystem/network side effects, no require_unlocked gate).
    """
    from .jobs import job_list

    return job_list()
