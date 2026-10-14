"""Read-only data-source abstraction for relationship discovery.

Two implementations share one small interface:

* ``SqliteSource``  - standard-library sqlite3, opened read-only (tests and local analysis).
* ``OdbcSource``    - a thin wrapper over the project's existing named ODBC connections
  (``MCP_DB_<NAME>``), opened read-only. The SQL it sends is produced by the pure functions
  at the top of this module so it can be unit-tested without any database.

Every read goes through ``rows_fetched`` so callers (and tests) can prove how many rows were
actually transferred; the discovery code never scans a whole large table.
"""

from __future__ import annotations

import random
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

#: SQL Server allows 2100 bound parameters per statement; stay well under it.
MAX_IN_BATCH = 500


class BudgetExceeded(Exception):
    """A query or loop ran past its time budget and was abandoned."""


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str


# --------------------------------------------------------------------------- SQL builders
def quote_ident(dialect: str, name: str) -> str:
    """Quote one identifier. For mssql a ``schema.table`` name becomes ``[schema].[table]``."""
    if dialect == "mssql":
        parts = name.split(".", 1) if "." in name else [name]
        return ".".join("[" + p.replace("]", "]]") + "]" for p in parts)
    return '"' + name.replace('"', '""') + '"'


def quote_column(dialect: str, name: str) -> str:
    if dialect == "mssql":
        return "[" + name.replace("]", "]]") + "]"
    return '"' + name.replace('"', '""') + '"'


def sql_select_in(dialect: str, table: str, column: str, n_values: int) -> str:
    """``SELECT DISTINCT col FROM t WHERE col IN (?, ?, ...)`` for a batch of ``n_values``."""
    if n_values < 1 or n_values > MAX_IN_BATCH:
        raise ValueError(f"IN batch must hold 1..{MAX_IN_BATCH} values, got {n_values}")
    c = quote_column(dialect, column)
    marks = ",".join("?" * n_values)
    return f"SELECT DISTINCT {c} FROM {quote_ident(dialect, table)} WHERE {c} IN ({marks})"


def sql_rowcount_mssql() -> str:
    """Approximate row count from the catalogue (no table scan). One bound parameter: name."""
    return (
        "SELECT SUM(row_count) FROM sys.dm_db_partition_stats "
        "WHERE object_id = OBJECT_ID(?) AND index_id IN (0, 1)"
    )


def sql_tables_mssql() -> str:
    return (
        "SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_SCHEMA, TABLE_NAME"
    )


def sql_columns_mssql() -> str:
    return (
        "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION"
    )


def sql_sample_mssql(table: str, columns: Sequence[str], n: int, rows_est: Optional[int],
                     use_tablesample: bool = True) -> str:
    """A light sample: ``TOP n`` over a ``TABLESAMPLE`` page sample (no ORDER BY NEWID()).

    ``TABLESAMPLE`` reads a random subset of pages, so the cost tracks the sample, not the
    table. It is skipped for small or unknown-size tables (plain ``TOP n``).
    """
    if n < 1:
        raise ValueError("sample size must be positive")
    cols = ", ".join(quote_column("mssql", c) for c in columns)
    base = f"SELECT TOP ({int(n)}) {cols} FROM {quote_ident('mssql', table)}"
    if use_tablesample and rows_est and rows_est > n * 4:
        pct = min(100.0, max(0.001, 100.0 * n * 2.0 / rows_est))
        return f"{base} TABLESAMPLE ({pct:.4f} PERCENT)"
    return base


# --------------------------------------------------------------------------- base class
class Source:
    """Minimal read-only interface. Subclasses must not execute anything but SELECTs."""

    kind = "abstract"
    dialect = "sqlite"

    def __init__(self) -> None:
        self.rows_fetched = 0
        self.queries_run = 0

    def list_tables(self) -> list[str]:
        raise NotImplementedError

    def columns(self, table: str) -> list[ColumnInfo]:
        raise NotImplementedError

    def approx_rows(self, table: str) -> Optional[int]:
        raise NotImplementedError

    def sample(self, table: str, columns: Sequence[str], n: int,
               seed: int = 0) -> tuple[list[tuple], bool]:
        """Up to ``n`` rows of ``columns``. Returns ``(rows, complete)``; ``complete`` is True
        only when the whole table was read (so the rows are exact, not a sample)."""
        raise NotImplementedError

    def select_in(self, table: str, column: str, values: Sequence[Any],
                  timeout_s: Optional[float] = None) -> list[Any]:
        """Distinct values of ``column`` that are among ``values`` (at most MAX_IN_BATCH)."""
        raise NotImplementedError

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- SQLite
class SqliteSource(Source):
    kind = "sqlite"
    dialect = "sqlite"

    def __init__(self, path: str) -> None:
        super().__init__()
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        self._con = sqlite3.connect(uri, uri=True)
        self._con.execute("PRAGMA query_only = ON")
        self.path = str(path)

    def _run(self, sql: str, params: Sequence[Any] = (), timeout_s: Optional[float] = None) -> list[tuple]:
        deadline = None if timeout_s is None else time.monotonic() + max(0.0, timeout_s)
        if deadline is not None:
            self._con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 2000)
        try:
            rows = self._con.execute(sql, tuple(params)).fetchall()
        except sqlite3.OperationalError as exc:
            if "interrupt" in str(exc).lower():
                raise BudgetExceeded("query exceeded its time budget") from exc
            raise
        finally:
            if deadline is not None:
                self._con.set_progress_handler(None, 0)
        self.queries_run += 1
        self.rows_fetched += len(rows)
        return rows

    def list_tables(self) -> list[str]:
        rows = self._run(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [r[0] for r in rows]

    def columns(self, table: str) -> list[ColumnInfo]:
        rows = self._run(f"PRAGMA table_info({quote_ident('sqlite', table)})")
        return [ColumnInfo(r[1], r[2] or "") for r in rows]

    def approx_rows(self, table: str) -> Optional[int]:
        t = quote_ident("sqlite", table)
        try:
            row = self._run(f"SELECT MAX(rowid) FROM {t}")[0]
            return int(row[0] or 0)
        except sqlite3.OperationalError:  # WITHOUT ROWID table
            return int(self._run(f"SELECT COUNT(*) FROM {t}")[0][0])

    def sample(self, table, columns, n, seed=0):
        t = quote_ident("sqlite", table)
        cols = ", ".join(quote_column("sqlite", c) for c in columns)
        nrows = self.approx_rows(table) or 0
        if nrows <= n:
            rows = self._run(f"SELECT {cols} FROM {t} LIMIT ?", (n + 1,))
            return rows[:n], len(rows) <= n
        try:
            lo = self._run(f"SELECT MIN(rowid) FROM {t}")[0][0]
            hi = self._run(f"SELECT MAX(rowid) FROM {t}")[0][0]
            span = hi - lo + 1
            rng = random.Random(f"{seed}:{table}")
            picks = rng.sample(range(lo, hi + 1), min(span, int(n * 1.2) + 1))
        except sqlite3.OperationalError:
            return self._run(f"SELECT {cols} FROM {t} LIMIT ?", (n,)), False
        out: list[tuple] = []
        for i in range(0, len(picks), MAX_IN_BATCH):
            chunk = picks[i:i + MAX_IN_BATCH]
            marks = ",".join("?" * len(chunk))
            out.extend(self._run(f"SELECT {cols} FROM {t} WHERE rowid IN ({marks})", chunk))
            if len(out) >= n:
                break
        return out[:n], False

    def select_in(self, table, column, values, timeout_s=None):
        if not values:
            return []
        sql = sql_select_in("sqlite", table, column, len(values))
        return [r[0] for r in self._run(sql, list(values), timeout_s)]

    def close(self) -> None:
        self._con.close()


# --------------------------------------------------------------------------- ODBC
class OdbcSource(Source):
    """Named, read-only ODBC connection (``MCP_DB_<NAME>``). Kept deliberately thin: the SQL
    comes from the pure builders above. Not exercised by CI (no database there)."""

    kind = "odbc"
    dialect = "mssql"

    def __init__(self, name: str, login_timeout: int = 30, query_timeout: int = 30) -> None:
        super().__init__()
        from tools.odbc_ops import _connect  # reuse the existing read-only connector

        self._con = _connect(name, timeout=login_timeout)
        self._query_timeout = query_timeout
        self._rows_cache: dict[str, Optional[int]] = {}

    def _run(self, sql: str, params: Sequence[Any] = (), timeout_s: Optional[float] = None,
             max_rows: Optional[int] = None) -> list[tuple]:
        self._con.timeout = max(1, int(round(timeout_s))) if timeout_s is not None else self._query_timeout
        cur = self._con.cursor()
        try:
            cur.execute(sql, *params)
            rows = cur.fetchmany(max_rows) if max_rows else cur.fetchall()
        except Exception as exc:  # pyodbc.Error subclasses; HYT00 is the query timeout
            if "HYT00" in str(exc) or "timeout" in str(exc).lower():
                raise BudgetExceeded("query exceeded its time budget") from exc
            raise
        finally:
            cur.close()
        rows = [tuple(r) for r in rows]
        self.queries_run += 1
        self.rows_fetched += len(rows)
        return rows

    def list_tables(self) -> list[str]:
        return [f"{s}.{t}" for s, t in self._run(sql_tables_mssql())]

    @staticmethod
    def _split(table: str) -> tuple[str, str]:
        schema, _, name = table.partition(".")
        return (schema, name) if name else ("dbo", table)

    def columns(self, table: str) -> list[ColumnInfo]:
        schema, name = self._split(table)
        return [ColumnInfo(r[0], r[1]) for r in self._run(sql_columns_mssql(), [schema, name])]

    def approx_rows(self, table: str) -> Optional[int]:
        if table not in self._rows_cache:
            schema, name = self._split(table)
            rows = self._run(sql_rowcount_mssql(), [quote_ident("mssql", f"{schema}.{name}")])
            self._rows_cache[table] = int(rows[0][0]) if rows and rows[0][0] is not None else None
        return self._rows_cache[table]

    def sample(self, table, columns, n, seed=0):
        est = self.approx_rows(table)
        sql = sql_sample_mssql(table, columns, n + 1, est)
        rows = self._run(sql, max_rows=n + 1)
        if not rows and est:  # TABLESAMPLE can come back empty on tiny page counts
            rows = self._run(sql_sample_mssql(table, columns, n + 1, est, use_tablesample=False),
                             max_rows=n + 1)
        complete = est is not None and est <= n and len(rows) <= n
        return rows[:n], complete

    def select_in(self, table, column, values, timeout_s=None):
        if not values:
            return []
        sql = sql_select_in("mssql", table, column, len(values))
        return [r[0] for r in self._run(sql, list(values), timeout_s)]

    def close(self) -> None:
        self._con.close()


def open_source(spec: str) -> Source:
    """``sqlite:<path>`` or ``odbc:<NAME>`` (a name defined as ``MCP_DB_<NAME>``)."""
    kind, _, rest = spec.partition(":")
    if kind == "sqlite" and rest:
        return SqliteSource(rest)
    if kind == "odbc" and rest:
        return OdbcSource(rest)
    raise ValueError("source must look like 'sqlite:<path>' or 'odbc:<NAME>'")


_TYPE_RULES = (
    (re.compile(r"int|serial|bit|bool", re.I), "int"),
    (re.compile(r"char|text|clob|string|uniqueid|guid|enum", re.I), "text"),
    (re.compile(r"dec|num|real|floa|doub|money|number", re.I), "num"),
    (re.compile(r"date|time|stamp", re.I), "date"),
    (re.compile(r"blob|binary|image|bytea", re.I), "binary"),
)


def type_family(raw: str) -> str:
    """Coarse family of a declared type: int / num / text / date / binary / any."""
    for pat, fam in _TYPE_RULES:
        if pat.search(raw or ""):
            return fam
    return "any"
