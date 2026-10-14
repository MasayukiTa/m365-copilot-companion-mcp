"""SQL sources: a tiny read-only abstraction over SQLite (tests, local files) and ODBC."""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

from .normalize import Result

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_ROWS = 100000
_LEAD = re.compile(r"^\s*(?:--[^\n]*\n|/\*.*?\*/|\s)*", re.S)


class SourceError(Exception):
    """Raised when a statement cannot be executed. ``kind`` is 'timeout', 'rejected' or 'error'."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def check_read_only(sql: str) -> None:
    body = _LEAD.sub("", sql or "", count=1)
    first = body.split(None, 1)[0].lower() if body.strip() else ""
    if first not in ("select", "with", "values"):
        raise SourceError("rejected", "only SELECT / WITH statements are executed")


class SqlSource:
    name = "source"

    def execute(self, sql: str, timeout: float = DEFAULT_TIMEOUT, max_rows: int = DEFAULT_MAX_ROWS) -> Result:
        raise NotImplementedError


class SqliteSource(SqlSource):
    def __init__(self, path: str):
        self.path = str(path)
        self.name = "sqlite:" + Path(self.path).name
        if not Path(self.path).is_file():
            raise SourceError("error", "SQLite file not found: %s" % self.path)

    def execute(self, sql: str, timeout: float = DEFAULT_TIMEOUT, max_rows: int = DEFAULT_MAX_ROWS) -> Result:
        check_read_only(sql)
        uri = Path(self.path).resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        deadline = time.monotonic() + timeout
        timed_out = []

        def _tick() -> int:
            if time.monotonic() > deadline:
                timed_out.append(1)
                return 1
            return 0

        try:
            con.execute("PRAGMA query_only = ON")
            con.set_progress_handler(_tick, 10000)
            cur = con.execute(sql)
            cols = [d[0] for d in (cur.description or [])]
            rows = [list(r) for r in cur.fetchmany(max_rows)]
            return Result(cols, rows)
        except sqlite3.Error as e:
            if timed_out:
                raise SourceError("timeout", "query timeout after %.0f s" % timeout)
            raise SourceError("error", "%s: %s" % (type(e).__name__, e))
        finally:
            con.close()


class OdbcSource(SqlSource):
    """Named ODBC connection from the existing connection registry, opened read-only.

    Reuses ``tools.odbc_ops`` (connections are ``MCP_DB_<NAME>`` environment variables). This is
    a command-line path for the operator; it is not exposed as an MCP tool.
    """

    def __init__(self, connection: str):
        self.connection = connection
        self.name = "odbc:" + connection

    def execute(self, sql: str, timeout: float = DEFAULT_TIMEOUT, max_rows: int = DEFAULT_MAX_ROWS) -> Result:
        check_read_only(sql)
        try:
            from tools import odbc_ops
        except Exception as e:  # pragma: no cover - environment specific
            raise SourceError("error", "ODBC support unavailable: %s" % e)
        try:  # pragma: no cover - needs a real driver
            con = odbc_ops._connect(self.connection, timeout=int(timeout))
            try:
                try:
                    con.timeout = int(timeout)
                except Exception:
                    pass
                cur = con.cursor()
                cur.execute(sql)
                if cur.description is None:
                    return Result([], [])
                cols = [d[0] for d in cur.description]
                rows = [list(r) for r in cur.fetchmany(max_rows)]
                return Result(cols, rows)
            finally:
                con.close()
        except SourceError:
            raise
        except Exception as e:  # pragma: no cover
            msg = "%s: %s" % (type(e).__name__, e)
            kind = "timeout" if re.search(r"timeout|timed out|HYT00|HYT01", msg, re.I) else "error"
            raise SourceError(kind, msg)


def open_source(spec: Optional[str]) -> Optional[SqlSource]:
    """Parse ``sqlite:<path>`` or ``odbc:<name>``."""
    if not spec:
        return None
    kind, _, rest = spec.partition(":")
    if kind.lower() == "sqlite" and rest:
        return SqliteSource(rest)
    if kind.lower() == "odbc" and rest:
        return OdbcSource(rest)
    raise ValueError("source must look like sqlite:<path> or odbc:<name>, got %r" % spec)
