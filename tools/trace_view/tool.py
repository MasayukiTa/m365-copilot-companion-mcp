# -*- coding: utf-8 -*-
"""The MCP tool: a Markdown summary of one time window's traces."""
from __future__ import annotations

from tools.security import require_unlocked

_MAX_CHARS = 14000


def trace_session(since: str, until: str, session: str = "") -> str:
    """Per-session timeline of tool calls between two times: gaps, refusals, fleet_submit outcomes.

    Read-only. Arguments and results are redacted (short head, length, hash). Needs an unlock,
    because it returns a history of what callers did.

    Args:
        since: Start, 'YYYY-MM-DD HH:MM[:SS]' in JST.
        until: End, same format.
        session: Optional session id prefix.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        from . import report as _report
        from .timeutil import parse_time, parse_tz
        tz = parse_tz("JST")
        t0, t1 = parse_time(since, tz), parse_time(until, tz)
        if t0 is None or t1 is None:
            return "[trace_session error: since and until are both required]"
        if t0 > t1:
            return "[trace_session error: since is after until]"
        if t1 - t0 > 7 * 86400:
            return "[trace_session error: the window is longer than 7 days; narrow it]"
        rep = _report.make_report(None, t0, t1, (session or "").strip() or None)
        text = _report.render_markdown(rep, tz, "ja", max_steps=40, max_sessions=8)
        if len(text) > _MAX_CHARS:
            text = text[:_MAX_CHARS] + "\n\n(...cut at %d characters; narrow the window or give a session)" % _MAX_CHARS
        return text
    except Exception as exc:
        return "[trace_session error: %s: %s]" % (type(exc).__name__, str(exc)[:200])
