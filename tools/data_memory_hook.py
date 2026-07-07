"""Auto-accumulation hook for DB exploration (Phase 2 of the data-intelligence plan).

After a SUCCESSFUL odbc_query / odbc_tables / odbc_columns call, this module
opt-in records what was learned into the EXISTING procedural memory store
(tools/procedural_memory.py) so the next exploration of the same DB/theme can
start from memory (via procedural_memory_search) instead of re-discovering it
from scratch. This module is not itself a tool -- it is called from inside
tools/odbc_ops.py, right before each read tool returns.

Env contract (ASCII only):
    MCP_DATA_MEMORY_AUTO=1
        Turn auto-recording on. Any other value (including unset) is OFF,
        which means zero behavior change for odbc_ops callers -- this whole
        module becomes a no-op. Read from os.environ at call time (not at
        import time), so tests and callers can flip it per-call.
    MCP_DATA_MEMORY_TABLE_TOKENS_MAX (optional, default 8)
        Cap on how many SCREAMING_SNAKE_CASE "table-ish" tokens get pulled out
        of a SQL statement into tags for record_query.

Import safety: this module never imports pyodbc or pandas, so importing it
can never fail for lack of a DB driver. procedural_memory is imported lazily
inside _save() for the same reason odbc_ops.py imports pyodbc lazily.

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
response) as "skipped" and swallows it quietly -- it never surfaces the
locked state as an error and never raises. Only the SQL text, table/column
NAMES, and a short count summary are ever stored -- never full result rows.
"""
from __future__ import annotations

import os
import re
from typing import Optional

_TABLE_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9_]{4,}\b")
_WS_RE = re.compile(r"\s+")

# Fixed column offsets mirroring the exact formatting in tools/odbc_ops.py
# (odbc_tables / odbc_columns). These are best-effort extraction helpers --
# if the formatting ever changes, extraction degrades gracefully (falls back
# to a truncated raw blob) rather than raising.
_TABLES_NAME_SLICE = slice(32, 62)   # f"{catalog:<14}  {schema:<14}  {name:<30}  {type}"
_COLUMNS_NAME_SLICE = slice(0, 30)   # f"{column_name:<30}  {type_name:<18}  ..."


def _auto_enabled() -> bool:
    return os.environ.get("MCP_DATA_MEMORY_AUTO", "0") == "1"


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
    """Up to `max_tokens` distinct SCREAMING_SNAKE_CASE-ish tokens as "table:<lower>" tags."""
    cap = max_tokens if max_tokens is not None else _table_tokens_max()
    seen: list[str] = []
    for m in _TABLE_TOKEN_RE.finditer(text or ""):
        tok = m.group(0)
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
    """Call procedural_memory_save and swallow any deny/error response.

    procedural_memory_save is require_unlocked()-gated; when the caller isn't
    unlocked (or any other error occurs) it returns a bracketed string like
    "[locked ...]" or "[procedural_memory_save error: ...]" instead of
    raising. Either way we treat it as "skipped" here -- never surface it,
    never raise.
    """
    from .procedural_memory import procedural_memory_save

    procedural_memory_save(intent=intent, snippet=snippet, tags=tags, context=context)
    return None


def record_query(connection: str, sql: str, result_text: str) -> None:
    """Best-effort: after a successful odbc_query, remember the SQL for reuse.

    No-op unless MCP_DATA_MEMORY_AUTO=1 and result_text looks like a real
    success payload. Never raises.
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

    No-op unless MCP_DATA_MEMORY_AUTO=1 and result_text looks like a real
    success payload. Never raises.
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

    No-op unless MCP_DATA_MEMORY_AUTO=1 and result_text looks like a real
    success payload. Never raises.
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
