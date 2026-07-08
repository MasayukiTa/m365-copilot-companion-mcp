"""Auto-accumulation hook for DB exploration (Phase 2 of the data-intelligence plan).

After a SUCCESSFUL odbc_query / odbc_tables / odbc_columns call, this module
opt-in records what was learned into the EXISTING procedural memory store
(tools/procedural_memory.py) so the next exploration of the same DB/theme can
start from memory (via procedural_memory_search) instead of re-discovering it
from scratch. This module is not itself a tool -- it is called from inside
tools/odbc_ops.py, right before each read tool returns.

Env contract (ASCII only):
    MCP_DATA_MEMORY_AUTO
        OPT-OUT: auto-recording is ON by default (including when the var is
        unset entirely), so DB exploration self-accumulates into procedural
        memory with zero configuration. Set it to "0", "false", "no", or
        "off" (case-insensitive) to disable it -- any other value keeps it
        ON. Read from os.environ at call time (not at import time), so tests
        and callers can flip it per-call.
    MCP_DATA_MEMORY_TABLE_TOKENS_MAX (optional, default 8)
        Cap on how many SCREAMING_SNAKE_CASE "table-ish" tokens get pulled out
        of a SQL statement into tags for record_query.

Import safety: this module never imports pyodbc or pandas, so importing it
can never fail for lack of a DB driver. procedural_memory.py has the same
property (json/re/time/pathlib + .security only), so its SQL_STOPWORDS
constant is imported at module level here (single source of truth for
SQL-syntax-word filtering, shared with procedural_memory's own table-tag
extraction) -- but procedural_memory_save itself is still imported lazily
inside _save(), same reasoning as odbc_ops.py importing pyodbc lazily.

Failure safety: every public function (record_query / record_tables /
record_columns) is wrapped in its own try/except and NEVER raises and NEVER
returns anything the caller needs to check -- odbc_ops.py calls these purely
for their side effect, and the tool's own return value is always left
unchanged.

Privacy / gating: the actual write goes through procedural_memory_save,
which is require_unlocked()-gated. The odbc_query / odbc_tables /
odbc_columns read tools are intentionally UNGATED (openWorldHint, no
require_unlocked call) so a not-yet-unlocked remote client can call them.
When that happens, procedural_memory_save returns a "[locked ...]" string
instead of writing; this module treats that (and any other bracketed
response) as "skipped" and swallows it quietly to the CALLER -- it never
surfaces the locked state as an error and never raises. It IS tracked,
though: _save() increments module-level counters (_stats) so the failure is
no longer invisible, and data_memory_status() (an ungated, read-only tool)
reports them plus whether MCP_DATA_MEMORY_AUTO is on. Only the SQL text,
table/column NAMES, and a short count summary are ever stored -- never full
result rows.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

from .procedural_memory import SQL_STOPWORDS

_logger = logging.getLogger(__name__)

_TABLE_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9_]{4,}\b")
_WS_RE = re.compile(r"\s+")

# Process-lifetime counters for data_memory_status() -- visibility into the P2
# defect: odbc_query/odbc_tables/odbc_columns are intentionally UNGATED (see
# module docstring), but the auto-memory write behind them goes through
# procedural_memory_save, which IS require_unlocked()-gated. Before this fix a
# locked remote caller's DB exploration worked while every auto-memory write
# failed silently forever, with nothing to grep for. These counters (plus the
# data_memory_status() tool below) make that visible instead of a bare
# "memory never updates" with no signal why.
_stats = {"saved": 0, "skipped_locked": 0, "skipped_error": 0}
_locked_logged = False

# Fixed column offsets mirroring the exact formatting in tools/odbc_ops.py
# (odbc_tables / odbc_columns). These are best-effort extraction helpers --
# if the formatting ever changes, extraction degrades gracefully (falls back
# to a truncated raw blob) rather than raising.
_TABLES_NAME_SLICE = slice(32, 62)   # f"{catalog:<14}  {schema:<14}  {name:<30}  {type}"
_COLUMNS_NAME_SLICE = slice(0, 30)   # f"{column_name:<30}  {type_name:<18}  ..."


def _auto_enabled() -> bool:
    """OPT-OUT: ON by default (unset counts as ON). Only an explicit
    "0"/"false"/"no"/"off" (case-insensitive) turns it off."""
    return os.environ.get("MCP_DATA_MEMORY_AUTO", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _table_tokens_max() -> int:
    try:
        return int(os.environ.get("MCP_DATA_MEMORY_TABLE_TOKENS_MAX", "8"))
    except Exception:
        return 8


def _looks_like_error_or_empty(result_text: str) -> bool:
    """True when result_text is not a real success payload worth remembering."""
    if not isinstance(result_text, str) or not result_text.strip():
        return True
    stripped = result_text.strip()
    if stripped.startswith("[") and " error:" in stripped:
        return True
    if stripped.startswith("(no ") or stripped.startswith("(statement returned no result set"):
        return True
    return False


def _extract_table_tags(text: str, max_tokens: Optional[int] = None) -> list[str]:
    """Up to `max_tokens` distinct SCREAMING_SNAKE_CASE-ish tokens as "table:<lower>" tags.

    Tokens that are pure SQL syntax (SELECT, GROUP, HAVING, DATEADD, ...) or too
    short (< 4 chars) to plausibly be a real table/column name are dropped before
    being tagged, same filtering procedural_memory.py's own _extract_table_tags
    applies to markdown-import chunks -- SQL_STOPWORDS is the single source of
    truth for the stopword list (imported from procedural_memory, not duplicated).
    """
    cap = max_tokens if max_tokens is not None else _table_tokens_max()
    seen: list[str] = []
    for m in _TABLE_TOKEN_RE.finditer(text or ""):
        tok = m.group(0)
        if tok.upper() in SQL_STOPWORDS or len(tok) < 4:
            continue
        if tok not in seen:
            seen.append(tok)
        if len(seen) >= cap:
            break
    return [f"table:{t.lower()}" for t in seen]


def _line_count(result_text: str) -> int:
    return len([ln for ln in (result_text or "").splitlines() if ln.strip()])


def _extract_table_names_from_listing(result_text: str) -> list[str]:
    """Pull just the `name` column out of an odbc_tables-formatted listing."""
    names: list[str] = []
    for line in (result_text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("---") or stripped.lower().startswith("catalog"):
            continue
        if len(line) < _TABLES_NAME_SLICE.stop:
            continue
        name = line[_TABLES_NAME_SLICE].strip()
        if name:
            names.append(name)
    return names


def _extract_column_names_from_listing(result_text: str) -> list[str]:
    """Pull just the `column` name column out of an odbc_columns-formatted listing."""
    names: list[str] = []
    for line in (result_text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("---") or stripped.lower().startswith("column"):
            continue
        name = line[_COLUMNS_NAME_SLICE].strip()
        if name:
            names.append(name)
    return names


def _save(intent: str, snippet: str, tags: str, context: str) -> None:
    """Call procedural_memory_save, track the outcome in _stats, and swallow
    any deny/error response so callers never see it and never raise.

    procedural_memory_save is require_unlocked()-gated; when the caller isn't
    unlocked (or any other error occurs) it returns a bracketed string like
    "[locked ...]" or "[procedural_memory_save error: ...]" instead of
    raising. Either way the CALLER (record_query/record_tables/record_columns)
    still never sees it and never raises -- but the outcome is no longer
    invisible: _stats (and data_memory_status()) now make it visible, and the
    first locked-skip logs one ASCII line so it shows up in server logs too.
    """
    global _locked_logged
    from .procedural_memory import procedural_memory_save

    result = procedural_memory_save(intent=intent, snippet=snippet, tags=tags, context=context)
    if isinstance(result, str) and result.startswith("[locked"):
        _stats["skipped_locked"] += 1
        if not _locked_logged:
            _locked_logged = True
            _logger.warning(
                "data_memory_hook: auto-memory save skipped (client locked); "
                "saves will resume after unlock"
            )
    elif isinstance(result, str) and result.startswith("[") and " error:" in result:
        _stats["skipped_error"] += 1
    else:
        _stats["saved"] += 1
    return None


def data_memory_status() -> str:
    """Read-only status of the DB-exploration auto-memory hook (no unlock needed).

    Auto-recording defaults ON (opt-out): reports whether MCP_DATA_MEMORY_AUTO
    is on and this PROCESS's save/skip counters, so a silent failure mode
    becomes visible: odbc_query / odbc_tables / odbc_columns are intentionally
    ungated, so a not-yet-unlocked remote client's DB exploration works fine --
    but the auto-memory write behind it goes through procedural_memory_save,
    which IS require_unlocked()-gated, so every such write was previously
    failing silently forever ("memory never updates" with no visible cause).
    This tool itself stays ungated (read-only, same reasoning as the odbc_*
    read tools) so it's reachable even while locked -- that's the point: you
    can check status before/without ever unlocking.
    """
    auto_on = _auto_enabled()
    saved = _stats["saved"]
    skipped_locked = _stats["skipped_locked"]
    skipped_error = _stats["skipped_error"]
    raw_env = os.environ.get("MCP_DATA_MEMORY_AUTO")
    raw_display = raw_env if raw_env is not None else "(unset, default ON)"
    lines = [
        "auto: %s (MCP_DATA_MEMORY_AUTO=%s)" % ("ON" if auto_on else "OFF", raw_display),
        "saved=%d skipped_locked=%d skipped_error=%d (this process, since start)"
        % (saved, skipped_locked, skipped_error),
    ]
    if skipped_locked > 0:
        lines.append(
            "hint: skipped_locked > 0 means auto-memory writes are being silently "
            "denied by procedural_memory_save's unlock gate -- call "
            "unlock(password='<password>') on this client to resume them."
        )
    elif not auto_on:
        lines.append(
            "hint: auto-recording is OFF (opted out). It defaults ON -- unset "
            "MCP_DATA_MEMORY_AUTO, or set it to anything other than "
            "0/false/no/off, to turn it back on."
        )
    return "\n".join(lines)


def record_query(connection: str, sql: str, result_text: str) -> None:
    """Best-effort: after a successful odbc_query, remember the SQL for reuse.

    No-op only when auto-recording is explicitly disabled (MCP_DATA_MEMORY_AUTO
    set to 0/false/no/off) or result_text doesn't look like a real success
    payload. ON by default. Never raises.
    """
    try:
        if not _auto_enabled():
            return None
        if _looks_like_error_or_empty(result_text):
            return None
        sql_text = sql or ""
        collapsed = _WS_RE.sub(" ", sql_text.strip())
        intent = collapsed[:80] or f"query on {connection}"
        tags_list = ["db", "auto", f"connection:{connection}"] + _extract_table_tags(sql_text)
        tags = ",".join(tags_list)
        context = f"returned {_line_count(result_text)} lines"
        _save(intent=intent, snippet=sql_text, tags=tags, context=context)
        return None
    except Exception:
        return None


def record_tables(connection: str, result_text: str) -> None:
    """Best-effort: after a successful odbc_tables, remember the object list.

    No-op only when auto-recording is explicitly disabled (MCP_DATA_MEMORY_AUTO
    set to 0/false/no/off) or result_text doesn't look like a real success
    payload. ON by default. Never raises.
    """
    try:
        if not _auto_enabled():
            return None
        if _looks_like_error_or_empty(result_text):
            return None
        names = _extract_table_names_from_listing(result_text)
        intent = f"tables in {connection}"
        snippet = ", ".join(names[:50]) if names else result_text[:200]
        tags = ",".join(["db", "auto", f"connection:{connection}", "catalog"])
        context = f"{len(names)} objects"
        _save(intent=intent, snippet=snippet, tags=tags, context=context)
        return None
    except Exception:
        return None


def record_columns(connection: str, table: str, result_text: str) -> None:
    """Best-effort: after a successful odbc_columns, remember the column list.

    No-op only when auto-recording is explicitly disabled (MCP_DATA_MEMORY_AUTO
    set to 0/false/no/off) or result_text doesn't look like a real success
    payload. ON by default. Never raises.
    """
    try:
        if not _auto_enabled():
            return None
        if _looks_like_error_or_empty(result_text):
            return None
        names = _extract_column_names_from_listing(result_text)
        intent = f"columns of {table} ({connection})"
        snippet = ", ".join(names[:60]) if names else result_text[:200]
        tags = ",".join(
            ["db", "auto", f"connection:{connection}", f"table:{(table or '').lower()}"]
        )
        context = f"{len(names)} columns"
        _save(intent=intent, snippet=snippet, tags=tags, context=context)
        return None
    except Exception:
        return None
