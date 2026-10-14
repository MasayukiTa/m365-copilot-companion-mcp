# -*- coding: utf-8 -*-
"""MCP tool entry points of the semantic layer.

Every tool returns business data from the operator's private dictionary, so each one is gated
by ``require_unlocked()`` exactly like ``odbc_query``. They are read-only: they read the JSON
files named by MCP_SEMANTIC_DIR and never execute SQL. Results are JSON text.
"""
import json

from ..security import require_unlocked

from . import catalog as _catalog
from . import metric_sql as _metric_sql
from . import resolve as _resolve


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str)


def semantic_resolve(text: str, today: str = "") -> str:
    """Look up a request's words in the glossary and metrics; list the words with no definition.

    GATED: returns the operator's business dictionary; call unlock first if refused.

    Args:
        text: the request, in any wording (full/half width, synonyms and Japanese are handled).
        today: optional ISO date for relative dates such as "past 1 month" (default: real today).
    Returns JSON: matches (term/metric, meaning, resolves_to, caveats, status), undefined
    (words with NO entry: never define them yourself), periods (resolved dates), warnings.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        return _dump(_resolve.resolve(text, today=today or None))
    except ValueError as e:
        return _dump({"ok": False, "error": str(e)})


def metric_sql(name: str, params: dict = None, period: str = "", today: str = "") -> str:
    """Build a metric's SQL with ? bind variables (values are never placed in the SQL text).

    GATED: returns the operator's metric definitions; call unlock first if refused. The SQL is
    NOT executed; only a single SELECT / WITH statement is ever produced.

    Args:
        name: metric name or synonym from the dictionary.
        params: explicit parameter values, e.g. {"start": "2026-09-01", "end": "2026-10-01"}.
        period: relative date phrase that fills the start/end pair: "past 1 month", "last
            month", "this month", "last 7 days" (or Japanese). Ranges are start-inclusive,
            end-exclusive.
        today: optional ISO base date for the period (default: real today).
    Returns JSON: sql (with ?), bind (values in order), resolved_dates, caveats, status.
    """
    locked = require_unlocked()
    if locked:
        return locked
    return _dump(_metric_sql.metric_sql(name, params or {}, period=period, today=today or None))


def catalog_search(query: str, limit: int = 10) -> str:
    """Search the table catalogue (table/column names, meanings, value meanings, pitfalls).

    GATED: returns the operator's database structure notes; call unlock first if refused.

    Args:
        query: words in any language (Japanese and English handled).
        limit: maximum number of tables returned (1-50).
    Returns JSON: ranked tables with score, coverage and the places that matched.
    """
    locked = require_unlocked()
    if locked:
        return locked
    return _dump(_catalog.catalog_search(query, limit=limit))


def catalog_describe(table: str) -> str:
    """Describe one table or view: columns, value meanings, joins, pitfalls, performance notes.

    GATED: returns the operator's database structure notes; call unlock first if refused.

    Args:
        table: the table or view name as listed by catalog_search.
    """
    locked = require_unlocked()
    if locked:
        return locked
    return _dump(_catalog.catalog_describe(table))


def catalog_joins(a: str, b: str, max_hops: int = 3) -> str:
    """Known joins between two tables, and multi-hop join paths found breadth first.

    GATED: returns the operator's database structure notes; call unlock first if refused.

    Args:
        a: first table.
        b: second table.
        max_hops: longest path to search for (1-6).
    Returns JSON: direct joins, and paths (tables, join conditions, confidence per hop).
    """
    locked = require_unlocked()
    if locked:
        return locked
    return _dump(_catalog.catalog_joins(a, b, max_hops=max_hops))
