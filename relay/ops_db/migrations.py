"""Schema migrations for the ops database.

The schema version lives in ``PRAGMA user_version`` and is mirrored, with a
name and an applied timestamp, in the ``schema_migrations`` table. We do not
rely on ``CREATE TABLE IF NOT EXISTS`` alone to evolve the schema: each step
is an explicit function that upgrades version N-1 to N, so an existing
database picks up added columns/tables through the same path a fresh one
does.

``connect`` applies the pragmas the spec requires (WAL, busy_timeout=5000,
foreign_keys=ON) and then brings the database up to the latest version.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Callable

from . import SCHEMA_REVISION
from . import schema as _schema
from . import views as _views


def _pragmas(con: sqlite3.Connection) -> None:
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA foreign_keys=ON")


def _user_version(con: sqlite3.Connection) -> int:
    return int(con.execute("PRAGMA user_version").fetchone()[0])


def _set_user_version(con: sqlite3.Connection, v: int) -> None:
    # PRAGMA user_version does not accept a bound parameter.
    con.execute(f"PRAGMA user_version={int(v)}")


def _record(con: sqlite3.Connection, version: int, name: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO schema_migrations(version, name, applied_at) "
        "VALUES (?, ?, ?)",
        (version, name, time.time()),
    )


def _migrate_1(con: sqlite3.Connection) -> None:
    """v0 -> v1: create every base table, index, FTS table and view."""
    con.executescript(_schema.TABLES_SQL)
    con.executescript(_schema.INDEXES_SQL)
    con.executescript(_schema.FTS_SQL)
    con.executescript(_views.VIEWS_SQL)
    con.executemany(
        "INSERT OR IGNORE INTO reason_codes(code, category, description) "
        "VALUES (?, ?, ?)",
        _schema.REASON_CODES,
    )


# version -> (name, upgrade function from version-1 to version)
MIGRATIONS: dict[int, tuple[str, Callable[[sqlite3.Connection], None]]] = {
    1: ("initial schema", _migrate_1),
}


def apply_migrations(con: sqlite3.Connection, target: int = SCHEMA_REVISION) -> int:
    """Bring ``con`` up to ``target`` version, returning the final version.

    Runs each pending migration in order inside a transaction and records it
    in schema_migrations and user_version. Idempotent: re-running on an
    up-to-date database does nothing.
    """
    current = _user_version(con)
    for version in range(current + 1, target + 1):
        if version not in MIGRATIONS:
            raise RuntimeError(f"no migration defined for version {version}")
        name, fn = MIGRATIONS[version]
        with con:  # one transaction per step
            fn(con)
            _record(con, version, name)
            _set_user_version(con, version)
        current = version
    return current


def connect(path: str) -> sqlite3.Connection:
    """Open (creating if needed) the ops database at ``path`` and migrate it.

    The returned connection has the spec pragmas applied and is at the
    latest schema version.
    """
    con = sqlite3.connect(path)
    _pragmas(con)
    apply_migrations(con)
    return con
