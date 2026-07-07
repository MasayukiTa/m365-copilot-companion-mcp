import os
import re
from typing import Any, Optional

from .file_ops import _validate_path
from .security import require_unlocked

CONN_PREFIX = "MCP_DB_"
DEFAULT_TIMEOUT = 30
READ_VERBS = {"select", "with", "exec", "execute", "show", "describe", "desc"}


def _list_connections() -> dict[str, str]:
    """Read connection strings from MCP_DB_<NAME> env vars (set in .env)."""
    out: dict[str, str] = {}
    for key, value in os.environ.items():
        if key.startswith(CONN_PREFIX) and value:
            name = key[len(CONN_PREFIX):].lower()
            out[name] = value
    return out


def odbc_drivers() -> str:
    """List installed ODBC drivers on this PC.

    Useful to verify e.g. 'ODBC Driver 18 for SQL Server' is available before
    composing a connection string.
    """
    try:
        import pyodbc

        drivers = pyodbc.drivers()
        if not drivers:
            return "(no ODBC drivers found)"
        return "\n".join(f"  {d}" for d in drivers)
    except Exception as e:
        return f"[odbc_drivers error: {type(e).__name__}: {e}]"


def odbc_connections() -> str:
    """List named connections defined in .env via MCP_DB_<NAME>= entries.

    Connection strings themselves are not printed (may contain secrets); only
    names and the masked target are shown.
    """
    try:
        conns = _list_connections()
        if not conns:
            return (
                "(no named connections defined)\n"
                "Add to .env: MCP_DB_<NAME>=<full ODBC connection string>"
            )
        lines = ["name           target"]
        for name, conn in conns.items():
            server = _extract_kv(conn, "server") or _extract_kv(conn, "host") or "?"
            db = _extract_kv(conn, "database") or "?"
            trusted = "Yes" if re.search(r"trusted_connection\s*=\s*yes", conn, re.I) else "No"
            lines.append(f"{name:<14}  server={server}  db={db}  trusted={trusted}")
        return "\n".join(lines)
    except Exception as e:
        return f"[odbc_connections error: {type(e).__name__}: {e}]"


def _extract_kv(conn: str, key: str) -> Optional[str]:
    m = re.search(rf"\b{re.escape(key)}\s*=\s*([^;]+)", conn, re.I)
    return m.group(1).strip() if m else None


def _connect(connection_name_or_string: str, timeout: int = DEFAULT_TIMEOUT):
    import pyodbc

    conns = _list_connections()
    if connection_name_or_string.lower() in conns:
        cs = conns[connection_name_or_string.lower()]
    elif "=" in connection_name_or_string and ";" in connection_name_or_string:
        cs = connection_name_or_string
    else:
        names = ", ".join(sorted(conns)) or "(none)"
        raise ValueError(
            f"unknown connection {connection_name_or_string!r}. "
            f"Named connections in .env: {names}"
        )
    return pyodbc.connect(cs, timeout=timeout, readonly=True, autocommit=True)


def _is_read_only(query: str) -> bool:
    stripped = query.lstrip()
    # Drop leading line/block comments
    while stripped.startswith("--") or stripped.startswith("/*"):
        if stripped.startswith("--"):
            nl = stripped.find("\n")
            stripped = stripped[nl + 1:] if nl != -1 else ""
        else:
            end = stripped.find("*/")
            stripped = stripped[end + 2:] if end != -1 else ""
        stripped = stripped.lstrip()
    first = stripped.split(maxsplit=1)[0].lower() if stripped else ""
    return first in READ_VERBS


def odbc_query(
    connection: str,
    query: str,
    params: Optional[list[Any]] = None,
    max_rows: int = 200,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Execute a read-only SQL statement over ODBC and return a text table.

    Only statements starting with SELECT, WITH, EXEC, SHOW, DESCRIBE are
    accepted. The connection is opened with readonly=True; DML/DDL is rejected
    at the driver layer for compliant drivers and at the query verb layer here.

    Args:
        connection: Either a named connection (from .env MCP_DB_<NAME>=...) or
            a full ODBC connection string (must contain '=' and ';').
        query: SQL statement.
        params: Optional positional parameters bound with '?' placeholders.
        max_rows: Truncate result set to this many rows.
        timeout: Connection / login timeout in seconds.
    """
    try:
        if not _is_read_only(query):
            return "[odbc_query error: only SELECT/WITH/EXEC/SHOW/DESCRIBE allowed]"
        con = _connect(connection, timeout=timeout)
        try:
            cur = con.cursor()
            cur.execute(query, *(params or []))
            if cur.description is None:
                return "(statement returned no result set)"
            columns = [d[0] for d in cur.description]
            rows = cur.fetchmany(max_rows)
        finally:
            con.close()
        result = _format_table(columns, rows, max_rows)
        try:
            from .data_memory_hook import record_query
            record_query(connection, query, result)
        except Exception:
            pass
        return result
    except Exception as e:
        return f"[odbc_query error: {type(e).__name__}: {e}]"


def odbc_tables(
    connection: str,
    schema: Optional[str] = None,
    catalog: Optional[str] = None,
) -> str:
    """List tables and views available through an ODBC connection.

    Args:
        connection: Named connection or full connection string.
        schema: Optional schema name filter (e.g. 'dbo').
        catalog: Optional catalog/database name filter.
    """
    try:
        con = _connect(connection)
        try:
            cur = con.cursor()
            rows = list(cur.tables(catalog=catalog, schema=schema))
        finally:
            con.close()
        if not rows:
            return "(no tables visible to this user)"
        lines = [f"{'catalog':<14}  {'schema':<14}  {'name':<30}  type"]
        for r in rows:
            lines.append(
                f"{(r.table_cat or ''):<14}  {(r.table_schem or ''):<14}  "
                f"{(r.table_name or ''):<30}  {r.table_type or ''}"
            )
        lines.append(f"--- {len(rows)} object(s)")
        result = "\n".join(lines)
        try:
            from .data_memory_hook import record_tables
            record_tables(connection, result)
        except Exception:
            pass
        return result
    except Exception as e:
        return f"[odbc_tables error: {type(e).__name__}: {e}]"


def odbc_columns(
    connection: str,
    table: str,
    schema: Optional[str] = None,
    catalog: Optional[str] = None,
) -> str:
    """Show column metadata for one table or view."""
    try:
        con = _connect(connection)
        try:
            cur = con.cursor()
            rows = list(cur.columns(table=table, catalog=catalog, schema=schema))
        finally:
            con.close()
        if not rows:
            return f"(no columns found for {table!r})"
        lines = [f"{'column':<30}  {'type':<18}  size   nullable"]
        for r in rows:
            lines.append(
                f"{(r.column_name or ''):<30}  {(r.type_name or ''):<18}  "
                f"{r.column_size or '':<6}  {'YES' if r.nullable else 'NO'}"
            )
        lines.append(f"--- {len(rows)} column(s)")
        result = "\n".join(lines)
        try:
            from .data_memory_hook import record_columns
            record_columns(connection, table, result)
        except Exception:
            pass
        return result
    except Exception as e:
        return f"[odbc_columns error: {type(e).__name__}: {e}]"


def odbc_to_excel(
    connection: str,
    query: str,
    output_path: str,
    params: Optional[list[Any]] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Run a read-only query over ODBC and dump the full result set to .xlsx.

    Use for large result sets where text rendering is not useful.

    Args:
        connection: Named connection or full connection string.
        query: SELECT/WITH statement.
        output_path: .xlsx output path under the allowed base.
        params: Optional positional parameters.
        timeout: Connection timeout.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        import pandas as pd

        if not _is_read_only(query):
            return "[odbc_to_excel error: only SELECT/WITH/EXEC/SHOW/DESCRIBE allowed]"
        out = _validate_path(output_path)
        if out.suffix.lower() != ".xlsx":
            return "[odbc_to_excel error: output_path must end with .xlsx]"
        con = _connect(connection, timeout=timeout)
        try:
            df = pd.read_sql_query(query, con, params=params or None)
        finally:
            con.close()
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_excel(out, index=False)
        return f"Wrote {out} ({len(df)} rows, {len(df.columns)} cols)"
    except Exception as e:
        return f"[odbc_to_excel error: {type(e).__name__}: {e}]"


def _format_table(columns: list[str], rows: list, max_rows: int) -> str:
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
