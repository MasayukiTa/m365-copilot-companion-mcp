"""Open this product's SQLite stores WITHOUT a memory-mapped -shm file.

WHY (measured 2026-10-22). The fleet coordinator (python 3.10, sqlite3.dll 3.35.5) died hard,
with no traceback, three times in one afternoon when C: ran out of space. The Windows
Application log (Id 1000/1005, 17:36:01, 17:37:25, 17:37:30) names exception 0xc0000006
(STATUS_IN_PAGE_ERROR) raised inside sqlite3.dll while touching
.fleet\\sessions\\sessions.sqlite3-shm and .jobs\\jobs.sqlite3-shm.

In WAL mode SQLite keeps its wal-index in that -shm file and MAPS it into the process. When the
volume is full, touching a mapped page that cannot be backed raises an in-page I/O error, an
OS exception that Python cannot catch, so the whole process dies. With a rollback journal
(DELETE) there is no -shm file and no mapping: the same full disk surfaces as an ordinary
sqlite3.OperationalError ("database or disk is full") that callers can catch, so the worst case
is a pause, not a death.

Proof that the mapping is the only difference (python 3.10.0 / sqlite 3.35.5, the production
pair; the test relay/test_sqlite_no_shm_mapping.py repeats it): after WAL the process has
<db>-shm (32768 bytes) mapped, found with VirtualQuery + GetMappedFileNameW; after DELETE the
file does not exist and nothing is mapped.

WHY NOT locking_mode=EXCLUSIVE (it also avoids -shm). Measured on 3.35.5: a second connection
to the same file fails with "database is locked". These stores are opened by several
processes at once (coordinator, bridge, MCP server, local loop controller, the cockpit's
ledger_dump), so EXCLUSIVE is not valid for them.

COST (measured, 300 commits of a 2 KB row, synchronous=FULL, same machine): WAL 3.1 s,
DELETE 7.5 s, TRUNCATE 9.6 s. DELETE is the faster rollback mode here; these stores commit once
per turn or per job step, far below that rate. A reader now blocks a writer for the length of one
statement, which the existing busy timeouts (5 s and 10 s) already cover.
"""

from __future__ import annotations

import sqlite3

#: The journal mode these stores use. See the module docstring for why not WAL and not
#: TRUNCATE (measured there).
ROLLBACK_MODE = "DELETE"


def use_rollback_journal(conn: sqlite3.Connection) -> str:
    """Make ``conn``'s database use a rollback journal. Returns the journal mode now in effect.

    A database created before this change is persistently in WAL mode, and leaving WAL needs
    the only connection to that file. If another process still holds it open the switch is
    refused at once (busy_timeout is 0 for this one statement, so an open call never stalls) and
    the mode stays "wal"; the next open retries, so the file converts the first time it is
    opened while nobody else has it. The return value tells the caller which case it is in.
    """
    mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    if mode == ROLLBACK_MODE.lower():
        return mode
    prior = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
    conn.execute("PRAGMA busy_timeout=0")
    try:
        row = conn.execute("PRAGMA journal_mode=%s" % ROLLBACK_MODE).fetchone()
        mode = str(row[0]).lower() if row else mode
    except sqlite3.OperationalError:
        pass  # locked by another connection: stay as we were, retry on the next open
    finally:
        conn.execute("PRAGMA busy_timeout=%d" % prior)
    return mode
