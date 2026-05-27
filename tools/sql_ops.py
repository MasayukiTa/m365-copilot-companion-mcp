import sqlite3
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from .file_ops import _validate_path
from .security import require_unlocked


def _open_ro(path: Path) -> sqlite3.Connection:
    """Open SQLite in read-only mode via URI."""
    uri = f"file:{quote(str(path))}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=10)


def sqlite_tables(database_path: str) -> str:
    """List tables and views in a SQLite database (read-only)."""
    try:
        p = _validate_path(database_path)
        if not p.is_file():
            return f"[sqlite_tables error: not a file: {p}]"
        with _open_ro(p) as con:
            cur = con.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
                "ORDER BY type, name"
            )
            rows = cur.fetchall()
        if not rows:
            return "(no user tables)"
        lines = [f"{'kind':<6}  name"]
        for kind, name in rows:
            lines.append(f"{kind:<6}  {name}")
        lines.append(f"--- {len(rows)} object(s)")
        return "\n".join(lines)
    except Exception as e:
        return f"[sqlite_tables error: {type(e).__name__}: {e}]"


def sqlite_schema(database_path: str, table: str) -> str:
    """Show CREATE statement and column list for one table or view."""
    try:
        p = _validate_path(database_path)
        if not p.is_file():
            return f"[sqlite_schema error: not a file: {p}]"
        with _open_ro(p) as con:
            ddl_row = con.execute(
                "SELECT sql FROM sqlite_master WHERE name=? AND type IN ('table','view')",
                (table,),
            ).fetchone()
            cols = con.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
        if not ddl_row:
            return f"[sqlite_schema: no table/view named {table!r}]"
        lines = [f"-- DDL", ddl_row[0], "", "-- columns:"]
        for cid, name, ctype, notnull, default, pk in cols:
            flags = []
            if pk:
                flags.append("pk")
            if notnull:
                flags.append("not null")
            if default is not None:
                flags.append(f"default={default}")
            lines.append(f"  {name:<24}  {ctype or '':<12}  {' '.join(flags)}")
        return "\n".join(lines)
    except Exception as e:
        return f"[sqlite_schema error: {type(e).__name__}: {e}]"


def _quote_ident(name: str) -> str:
    if not name or any(c in name for c in ' "[]();'):
        raise ValueError(f"invalid identifier: {name!r}")
    return f'"{name}"'


def sqlite_query(
    database_path: str,
    query: str,
    params: Optional[list[Any]] = None,
    max_rows: int = 200,
) -> str:
    """Run a read-only SELECT (or PRAGMA / EXPLAIN) against a SQLite database.

    Writes are blocked by opening the database in read-only mode.

    Args:
        database_path: SQLite database file under the allowed base.
        query: SQL statement. Only SELECT, WITH, PRAGMA, EXPLAIN are accepted.
        params: Optional positional parameters bound with ? placeholders.
        max_rows: Truncate result set to this many rows.
    """
    try:
        p = _validate_path(database_path)
        if not p.is_file():
            return f"[sqlite_query error: not a file: {p}]"
        stripped = query.lstrip().split(maxsplit=1)
        if not stripped:
            return "[sqlite_query error: empty query]"
        first = stripped[0].lower()
        if first not in {"select", "with", "pragma", "explain"}:
            return f"[sqlite_query error: only SELECT/WITH/PRAGMA/EXPLAIN allowed, got {first!r}]"
        with _open_ro(p) as con:
            cur = con.execute(query, params or [])
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(max_rows)
        if not columns:
            return "(no rows / no columns)"
        return _format_table(columns, rows, max_rows)
    except sqlite3.OperationalError as e:
        return f"[sqlite_query SQL error: {e}]"
    except Exception as e:
        return f"[sqlite_query error: {type(e).__name__}: {e}]"


def sqlite_to_excel(
    database_path: str,
    query: str,
    output_path: str,
    params: Optional[list[Any]] = None,
) -> str:
    """Run a SELECT and dump the full result set to an .xlsx file.

    Use when the result is large enough that text rendering is not useful.

    Args:
        database_path: SQLite file.
        query: SELECT (or WITH) statement.
        output_path: .xlsx output path.
        params: Optional ? parameters.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        import pandas as pd

        p = _validate_path(database_path)
        if not p.is_file():
            return f"[sqlite_to_excel error: not a file: {p}]"
        out = _validate_path(output_path)
        if out.suffix.lower() != ".xlsx":
            return "[sqlite_to_excel error: output_path must end with .xlsx]"
        first = query.lstrip().split(maxsplit=1)[0].lower()
        if first not in {"select", "with"}:
            return "[sqlite_to_excel error: only SELECT/WITH queries are allowed]"
        with _open_ro(p) as con:
            df = pd.read_sql_query(query, con, params=params or [])
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_excel(out, index=False)
        return f"Wrote {out} ({len(df)} rows, {len(df.columns)} cols)"
    except Exception as e:
        return f"[sqlite_to_excel error: {type(e).__name__}: {e}]"


def _format_table(columns: list[str], rows: list[tuple], max_rows: int) -> str:
    widths = [len(c) for c in columns]
    str_rows = []
    for row in rows:
        srow = ["" if v is None else str(v) for v in row]
        for i, cell in enumerate(srow):
            if len(cell) > widths[i]:
                widths[i] = min(len(cell), 40)
        str_rows.append(srow)
    header = "  ".join(c.ljust(widths[i]) for i, c in enumerate(columns))
    sep = "  ".join("-" * widths[i] for i in range(len(columns)))
    lines = [header, sep]
    for srow in str_rows:
        lines.append("  ".join(srow[i].ljust(widths[i])[:widths[i]] for i in range(len(columns))))
    if len(rows) == max_rows:
        lines.append(f"... (truncated at max_rows={max_rows})")
    lines.append(f"--- {len(rows)} row(s)")
    return "\n".join(lines)
