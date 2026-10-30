# -*- coding: utf-8 -*-
"""Tell apart which CLIENT started the MCP traffic in a window: Claude, the Copilot agent,
or another tool -- so "was it the copilot agent or Claude that did this?" has a recorded answer.

WHY THIS EXISTS. When the server and the tunnel went down and the repair tooling kept firing,
the question asked was not "what call failed" but "what STARTED this -- the copilot agent or
Claude?" (owner, 2026-10-xx: "copilotagent がやらかしてるのかと思ったわ。そいならええわ。claude
起因なら"). The evidence to answer it already lands in `mcp_inbound.jsonl`: every JSON-RPC
arrival carries the client's user-agent (`ua`), the session, and the client IP. What was
missing was the one step that turns a raw user-agent into "this was Claude" vs "this was the
Copilot agent" and counts them, which is all this module does.

WHAT DECIDES. The user-agent is the honest signal and it is stable in the real log (measured
2026-10-27 over 15,745 inbound rows, four distinct agents):

  * ``claude-code/... (claude-desktop, agent-sdk/...)``  -> Claude (claude).
  * ``codex-mcp-client/...``                             -> the Codex CLI agent (codex).
  * ``python-httpx*`` / ``Python-urllib*``               -> the connector-less relay, which is
    how a Copilot Studio / copilot-agent conversation reaches this server (copilot).
  * anything else                                        -> other (named, never guessed).

This is attribution of the REQUEST ORIGIN, the same ``ua``/``client_ip`` already stored; it
reads no body, argument, token or credential, and -- crucially -- it NEVER returns a raw
user-agent. A caller gets a category label and counts, so an origin report cannot become a
second place a user-agent string (which can carry a version or a hostname) lives.
"""
from __future__ import annotations

import re

#: Stable origin categories. English keys, like every other key in the trace report.
CLAUDE = "claude"
COPILOT = "copilot"
CODEX = "codex"
OTHER = "other"
UNKNOWN = "unknown"

ORIGINS = (CLAUDE, COPILOT, CODEX, OTHER, UNKNOWN)

#: Matched against the lower-cased user-agent, most specific first. A pattern that names a
#: concrete client wins over the generic HTTP-library patterns that mean "came through the
#: connector-less relay".
_RULES = (
    (CLAUDE, re.compile(r"claude[-/ ]|claude-desktop|anthropic")),
    (CODEX, re.compile(r"codex[-/ ]")),
    (COPILOT, re.compile(r"python-httpx|python-urllib|copilot|msedge|edg/|azure")),
)


def classify_origin(ua) -> str:
    """The origin category of one user-agent string.

    Empty / missing -> ``unknown`` (the row had no ua at all, not "some other client"). A
    non-empty ua that matches no rule -> ``other``. Never raises; never echoes the input.
    """
    text = str(ua or "").strip().lower()
    if not text:
        return UNKNOWN
    for origin, pattern in _RULES:
        if pattern.search(text):
            return origin
    return OTHER


def attribute_by_origin(inbound_rows) -> dict:
    """Count inbound MCP rows by origin category, with per-origin session and method detail.

    `inbound_rows` are rows as loaded from ``mcp_inbound.jsonl`` (see tools/obs_log.py and
    tools/trace_view/load.py): dicts that may carry ``ua``, ``session``, ``method`` and
    ``client_ip``. Returns::

        {
          "total": int,
          "origins": {
              <origin>: {
                  "rows": int,              # inbound rows from this origin
                  "sessions": int,          # distinct non-empty session ids
                  "client_ips": int,        # distinct non-empty client IPs
                  "methods": {method: count},
              },
              ...
          },
          "order": [<origin>, ...],        # origins present, in ORIGINS order
        }

    No raw user-agent, session id or IP is ever returned -- only categories and counts, so this
    is safe to render in a shared report. Never raises.
    """
    origins: dict = {}
    try:
        for row in inbound_rows or []:
            if not isinstance(row, dict):
                continue
            origin = classify_origin(row.get("ua"))
            bucket = origins.setdefault(
                origin, {"rows": 0, "_sessions": set(), "_ips": set(), "methods": {}})
            bucket["rows"] += 1
            sess = str(row.get("session") or "")
            if sess:
                bucket["_sessions"].add(sess)
            ip = str(row.get("client_ip") or "")
            if ip:
                bucket["_ips"].add(ip)
            method = str(row.get("method") or "")[:80]
            if method:
                bucket["methods"][method] = bucket["methods"].get(method, 0) + 1
    except Exception:
        pass
    out_origins = {}
    total = 0
    for origin, bucket in origins.items():
        rows = int(bucket["rows"])
        total += rows
        out_origins[origin] = {
            "rows": rows,
            "sessions": len(bucket["_sessions"]),
            "client_ips": len(bucket["_ips"]),
            "methods": dict(bucket["methods"]),
        }
    order = [o for o in ORIGINS if o in out_origins]
    # Any origin label not in the canonical list (there should be none) is appended stably.
    order += sorted(o for o in out_origins if o not in ORIGINS)
    return {"total": total, "origins": out_origins, "order": order}


def dominant_origin(summary) -> str:
    """The origin with the most rows in an `attribute_by_origin` result, or ``unknown`` when
    there is nothing to attribute. Ties break by ORIGINS order (claude before copilot before
    codex before other). Answers "so whose was it, mostly?" in one call."""
    try:
        origins = summary.get("origins") or {}
    except AttributeError:
        return UNKNOWN
    if not origins:
        return UNKNOWN
    rank = {o: i for i, o in enumerate(ORIGINS)}
    return min(origins,
               key=lambda o: (-int(origins[o].get("rows", 0)), rank.get(o, len(ORIGINS))))
