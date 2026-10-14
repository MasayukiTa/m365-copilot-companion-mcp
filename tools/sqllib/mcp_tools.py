"""MCP tools for the SQL example library.

All four are gated with require_unlocked(): they return the operator's own SQL. There is no
tool that takes a status. Promotion to "validated" happens by itself inside sql_example_use
when the recorded runs qualify; "certified" comes from sql_example_feedback(ok, source=user)
or the operator CLI. Automatic validation confirms the SQL runs and repeats, not that the
business logic is right.
"""
from __future__ import annotations

import json
from typing import Optional

from tools.security import require_unlocked

from . import match as _match
from . import store


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _err(tool: str, exc: Exception) -> str:
    return "[%s error: %s: %s]" % (tool, type(exc).__name__, exc)


def sql_examples_search(question: str, limit: int = 5, threshold: Optional[float] = None) -> str:
    """Find stored SQL examples for a question; says certified / validated / draft / no match.

    Args:
        question: the business question, in the user's own words.
        limit: maximum number of matches.
        threshold: minimum similarity 0..1 (default 0.5).
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        return _json(_match.match(question, limit=limit, threshold=threshold))
    except Exception as e:
        return _err("sql_examples_search", e)


def sql_example_save(question: str, sql: str, params: Optional[dict] = None,
                     tables: Optional[list] = None, notes: Optional[str] = None) -> str:
    """Save a question -> SELECT example. Always stored as an unverified draft.

    Args:
        question: the business question this SQL answers.
        sql: one SELECT/WITH statement; bind variables as :name.
        params: declaration of the :name parameters, e.g. {"start_date": "first day"}.
        tables: tables used (derived from the SQL when omitted).
        notes: free text.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        return _json(store.save(question, sql, params, tables, notes))
    except Exception as e:
        return _err("sql_example_save", e)


def sql_example_use(id: int, outcome: str, rowcount: Optional[int] = None,
                    ms: Optional[int] = None, result_hash: Optional[str] = None,
                    args_hash: Optional[str] = None) -> str:
    """Record one run of a stored example; enough identical successful runs auto-validate it.

    Args:
        id: entry id from sql_examples_search.
        outcome: ok | error | edited.
        rowcount: rows returned.
        ms: run time in milliseconds.
        result_hash: any stable fingerprint of the result (needed for re-run comparison).
        args_hash: fingerprint of the parameter values used (runs are compared per value set).
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        return _json(store.record_use(id, outcome, rowcount, ms, result_hash, args_hash))
    except Exception as e:
        return _err("sql_example_use", e)


def sql_example_feedback(id: int, verdict: str, note: Optional[str] = None,
                         source: str = "user") -> str:
    """Give light feedback on an example: ok (source=user certifies it) or ng (flags review).

    Args:
        id: entry id.
        verdict: ok | ng.
        note: why.
        source: user | auto.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        return _json(store.feedback(id, verdict, note, source))
    except Exception as e:
        return _err("sql_example_feedback", e)


SQLLIB_TOOLS = (sql_examples_search, sql_example_save, sql_example_use, sql_example_feedback)
