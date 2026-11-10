"""ops_db: operational metrics database (stage 1).

A separate, structured SQLite database that lets the fleet's operational
KPIs (infra stop rate, auto-recovery time, per-request final completion,
turn completion, availability, manual intervention) be produced with a
single SQL query, day/route/request-wise.

The existing sessions database is treated as a read-only input and is
never modified or copied. See README.md for the full design.
"""

from __future__ import annotations

OPS_SCHEMA_VERSION = 1

__all__ = ["OPS_SCHEMA_VERSION"]
