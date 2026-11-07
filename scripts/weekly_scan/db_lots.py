# -*- coding: utf-8 -*-
"""DB connection and lot-counting utilities, stratified by product.

CONVENTION REUSE. This does not invent a new DB access path. It reuses the server's
existing named-connection convention from ``tools.odbc_ops``: a connection is referred
to by NAME (e.g. ``ADAMS``), the actual connection string lives only in ``.env`` as
``MCP_DB_<NAME>=...`` and is read by ``tools.odbc_ops``; it is never read, printed, or
stored here. Authentication is Windows / Entra integrated (the account the server runs
as), exactly as ``tools.odbc_ops`` documents.

WHY COUNT BY PRODUCT. A weekly scan that counts lots across mixed products would let a
confound in: one product can flood the count while the product actually under test is
still short. So every count here is stratified by a product column and returned
per-product; the caller decides which stratum (the product under test) must reach the
registered lot requirement.

This module runs only COUNT/GROUP BY reads. It performs no judging and writes nothing
to the database. It never writes to H: or SharePoint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

#: A conservative identifier pattern for table / column names we are willing to splice
#: into SQL (ODBC COUNT ... GROUP BY cannot bind identifiers as parameters). Values are
#: still bound as parameters; only identifiers go through this gate, and anything that
#: does not match is refused rather than quoted-and-hoped.
_IDENT_RE = re.compile(r"^[A-Za-z_\u3040-\u30ff\u4e00-\u9fff][\w\u3040-\u30ff\u4e00-\u9fff]*$")


class DbLotsError(ValueError):
    """A lot-count request is mis-specified (bad identifier, missing connection, or the
    DB layer refused). Raised rather than returning 0, because a silent 0 reads as
    'enough lots were counted and there were none' -- the opposite of the truth."""


def _check_ident(kind: str, value: str) -> str:
    """Validate a SQL identifier (table or column). Returns it unchanged if safe.

    Identifiers cannot be passed as ODBC parameters, so they are spliced into the SQL
    text. To keep that safe we refuse anything that is not a plain identifier. A
    schema-qualified name (``schema.table``) is allowed by validating each part.
    """
    if not isinstance(value, str) or not value.strip():
        raise DbLotsError("%s must be a non-empty string" % kind)
    parts = value.split(".")
    for part in parts:
        if not _IDENT_RE.match(part):
            raise DbLotsError(
                "%s %r is not a plain identifier; refusing to splice it into SQL" % (kind, value)
            )
    return value


@dataclass(frozen=True)
class LotCount:
    """Lots counted for one product stratum."""

    product: Any
    lots: int


def _default_query_fn() -> Callable[..., str]:
    """Resolve the server's odbc_query at call time.

    Imported lazily so this module can be imported (and unit-tested) on a machine
    without pyodbc / without the server package on the path. Tests inject their own
    query_fn and never hit this.
    """
    try:
        from tools.odbc_ops import odbc_query  # type: ignore
    except Exception as exc:  # pragma: no cover - environment dependent
        raise DbLotsError(
            "tools.odbc_ops.odbc_query is unavailable in this environment: %s" % exc
        ) from exc
    return odbc_query


def _parse_count_table(text: str) -> list[tuple[str, str]]:
    """Parse the two-column text table that odbc_query renders into (col0, col1) rows.

    odbc_query returns a human text table, not structured rows. We parse the two
    columns (product, count) defensively: skip the header and any footer / separator
    lines, and keep only lines that end in an integer.
    """
    rows: list[tuple[str, str]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("---") or s.startswith("["):
            continue
        # The last whitespace-separated token must be an integer count.
        m = re.search(r"^(.*?)\s+(-?\d+)$", s)
        if not m:
            continue
        product = m.group(1).strip()
        count = m.group(2)
        # Skip a header row like 'product   count' where the 'count' is not numeric
        # (already excluded by the regex) or where product is a column label.
        rows.append((product, count))
    return rows


def count_lots_by_product(
    connection: str,
    table: str,
    lot_column: str,
    product_column: str,
    where: Optional[str] = None,
    params: Optional[list[Any]] = None,
    query_fn: Optional[Callable[..., str]] = None,
) -> list[LotCount]:
    """Count DISTINCT lots per product.

    Args:
        connection: Named connection (e.g. ``ADAMS``) or a full ODBC connection string,
            passed straight to odbc_query. The string is never read here.
        table: Table / view to count from (identifier; schema-qualified allowed).
        lot_column: Column identifying a lot (counted with COUNT(DISTINCT ...)).
        product_column: Column to stratify by (the product / \u54c1\u540d column).
        where: Optional raw SQL WHERE predicate (without the 'WHERE' keyword). Use
            ``?`` placeholders and pass ``params`` for every value -- values are bound,
            not spliced. A predicate with a literal is accepted but discouraged.
        params: Positional parameters bound to the ``?`` placeholders in ``where``.
        query_fn: Injection seam for tests; defaults to tools.odbc_ops.odbc_query.

    Returns:
        A list of LotCount, one per product stratum, in the order the DB returned.

    Raises:
        DbLotsError: on a bad identifier, or when the DB layer returned an error.
    """
    _check_ident("table", table)
    _check_ident("lot_column", lot_column)
    _check_ident("product_column", product_column)
    qf = query_fn or _default_query_fn()

    sql = (
        "SELECT %s AS product, COUNT(DISTINCT %s) AS lots FROM %s"
        % (product_column, lot_column, table)
    )
    if where:
        sql += " WHERE " + where
    sql += " GROUP BY %s ORDER BY %s" % (product_column, product_column)

    out = qf(connection, sql, params=params)
    if isinstance(out, str) and out.lstrip().startswith("[") and "error" in out.lower():
        raise DbLotsError("odbc_query refused or failed: %s" % out.strip())

    counts: list[LotCount] = []
    for product, count in _parse_count_table(out):
        if product.lower() == "product" and not count.lstrip("-").isdigit():
            continue
        try:
            counts.append(LotCount(product=product, lots=int(count)))
        except ValueError:
            continue
    return counts


def lots_for_product(
    counts: list[LotCount],
    product: Any,
) -> int:
    """Pick the lot count for one product from a stratified result. 0 if absent
    (the product genuinely has no lots counted). Comparison is by string value so a
    numeric/string product id does not silently miss.
    """
    key = str(product)
    for c in counts:
        if str(c.product) == key:
            return c.lots
    return 0


def lots_shortfall(
    counts: list[LotCount],
    product: Any,
    required: int,
) -> int:
    """How many MORE lots the given product needs to reach ``required``.

    Returns 0 when the product already meets or exceeds the requirement (never a
    negative number). ``required`` is the registered lot requirement read from the
    judge result's capacity diagnostics -- this function does not decide it.
    """
    if required < 0:
        raise DbLotsError("required lot count cannot be negative: %r" % required)
    have = lots_for_product(counts, product)
    gap = required - have
    return gap if gap > 0 else 0


def product_reached_requirement(
    counts: list[LotCount],
    product: Any,
    required: int,
) -> bool:
    """True when the product's counted lots meet or exceed the registered requirement.

    This is the gate a class-B/C scan uses to decide whether to fetch fresh
    confirmation data. It reports the count vs the registered requirement; it does
    not change the requirement and does not run the judge.
    """
    return lots_for_product(counts, product) >= required
