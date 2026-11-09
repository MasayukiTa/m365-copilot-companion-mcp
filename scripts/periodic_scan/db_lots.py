# -*- coding: utf-8 -*-
"""DB connection and lot-counting utilities (count stratified by a product column).

Purpose: for the periodic scan, decide whether a class-C analysis (too few lots)
has reached its required lot count now that confirmation lots have accumulated,
by counting distinct lots in a production table, stratified by a product column.

Design:
  - No connection name, table, or column is hardcoded. The logical ODBC
    connection NAME is read from the local scan profile (see :mod:`config`) or
    passed explicitly by the caller. The connection STRING, server and
    credentials live in the ODBC/.env layer the injected ``query_callable``
    owns; this module holds and prints none of them.
  - Raw measurement rows and personal identifiers are never read or returned;
    only per-stratum counts (data minimisation).
  - SQL VALUES are always passed as bind parameters (?), never inlined.

This module imports no query tool. It takes a ``query_callable`` and calls
``query_callable(connection=..., query=..., params=[...])``. In production the
``odbc_query`` tool is passed; unit tests pass a fake.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import config as _config

# Characters allowed in a stratification value. SQL binds the value, so injection
# cannot occur through it; this only rejects values that are not shaped like an
# identifier before they are counted.
_PRODUCT_RE = re.compile(r"^[0-9A-Za-z\u3040-\u30ff\u4e00-\u9fff_\-]+$")


def default_connection(profile: Optional[dict] = None) -> Optional[str]:
    """The logical ODBC connection name from the local profile, or None.

    Never returns a hardcoded business connection name. If the local profile
    does not set ``connection``, this returns None and the caller must pass
    ``connection=`` explicitly (or the call fails loudly) -- the module refuses
    to invent a real system name.
    """
    prof = profile if profile is not None else _config.load_profile()
    val = prof.get("connection")
    return str(val) if val else None


@dataclass
class LotCount:
    """One stratum's count."""

    product: str
    lots: int

    def __post_init__(self) -> None:
        if self.lots < 0:
            raise ValueError("lots must be >= 0")


@dataclass
class LotCountResult:
    """Stratified lot counts. Holds no raw rows."""

    table: str
    unit_column: str
    product_column: str
    per_product: list[LotCount] = field(default_factory=list)

    @property
    def total_lots(self) -> int:
        return sum(c.lots for c in self.per_product)

    def get(self, product: str) -> int:
        for c in self.per_product:
            if c.product == product:
                return c.lots
        return 0

    def meets_required(self, required_lots: int, product: str | None = None) -> bool:
        """Whether the required lots are met (per stratum if given, else total)."""
        have = self.get(product) if product is not None else self.total_lots
        return have >= required_lots


def _validate_identifier(name: str, kind: str) -> str:
    """Validate a table/column identifier (values bind; identifiers cannot).

    Identifiers come from the local config, not user input, but since they are
    spliced into SQL they are restricted to alphanumerics, a few punctuation
    characters and dots (schema.table).
    """
    if not name or not re.match(r"^[0-9A-Za-z_\.\[\]]+$", name):
        raise ValueError("unsafe %s identifier: %r" % (kind, name))
    return name


def count_lots_by_product(
    query_callable: Callable[..., Any],
    table: str,
    unit_column: str,
    product_column: str,
    products: list[str] | None = None,
    connection: Optional[str] = None,
    where_sql: str | None = None,
    where_params: list[Any] | None = None,
    result_parser: Callable[[Any], list[tuple[str, int]]] | None = None,
) -> LotCountResult:
    """Count distinct lots, stratified by product.

    Conceptual SQL:
        SELECT <product_column> AS product, COUNT(DISTINCT <unit_column>) AS lots
        FROM <table>
        [WHERE <where_sql> [AND <product_column> IN (?, ?, ...)]]
        GROUP BY <product_column>

    Parameters:
      query_callable : an ``odbc_query``-like callable taking
                       ``(connection, query, params)``.
      table          : target table/view. Validated as an identifier.
      unit_column    : the lot column (COUNT DISTINCT target).
      product_column : the stratification column.
      products       : optional list limiting which products to count.
      connection     : logical ODBC connection name. If None, taken from the
                       local profile; if still unset, a ValueError is raised
                       (the module never invents a real connection name).
      where_sql      : extra WHERE clause (values go in where_params as ?).
      where_params   : bind values for where_sql.
      result_parser  : converts query_callable's return into [(product, lots)].

    Returns: LotCountResult (per-stratum counts only; no raw rows).
    """
    conn = connection if connection is not None else default_connection()
    if not conn:
        raise ValueError(
            "no ODBC connection name: pass connection=, or set 'connection' in "
            ".config/scan_profile.json (see docs/CONFIG.md)"
        )

    _validate_identifier(table, "table")
    _validate_identifier(unit_column, "unit_column")
    _validate_identifier(product_column, "product_column")

    clauses: list[str] = []
    params: list[Any] = []
    if where_sql:
        clauses.append("(%s)" % where_sql)
        params.extend(where_params or [])

    if products:
        for p in products:
            if not _PRODUCT_RE.match(str(p)):
                raise ValueError("unsafe product value: %r" % p)
        placeholders = ", ".join("?" for _ in products)
        clauses.append("%s IN (%s)" % (product_column, placeholders))
        params.extend(products)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    query = (
        "SELECT %s AS product, COUNT(DISTINCT %s) AS lots FROM %s%s GROUP BY %s"
        % (product_column, unit_column, table, where, product_column)
    )

    raw = query_callable(connection=conn, query=query, params=params)

    if result_parser is not None:
        rows = result_parser(raw)
    else:
        rows = _default_parse_rows(raw)

    per_product = [LotCount(product=str(prod), lots=int(n)) for prod, n in rows]
    per_product.sort(key=lambda c: c.product)
    return LotCountResult(
        table=table,
        unit_column=unit_column,
        product_column=product_column,
        per_product=per_product,
    )


def _default_parse_rows(raw: Any) -> list[tuple[str, int]]:
    """Convert query_callable's return into [(product, lots)].

    Accepts:
      - [(product, lots), ...]              (iterable of tuples/lists)
      - [{'product': .., 'lots': ..}, ..]   (iterable of dicts)
    For a callable that returns only text, pass a result_parser.
    """
    out: list[tuple[str, int]] = []
    if raw is None:
        return out
    try:
        iterator = iter(raw)
    except TypeError as e:
        raise ValueError(
            "cannot parse query result; pass a result_parser for this query_callable"
        ) from e
    for row in iterator:
        if isinstance(row, dict):
            if "product" in row and "lots" in row:
                out.append((row["product"], row["lots"]))
                continue
            raise ValueError("row dict missing 'product'/'lots': %r" % (row,))
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            out.append((row[0], row[1]))
            continue
        raise ValueError("unrecognised row shape: %r" % (row,))
    return out


__all__ = [
    "default_connection",
    "LotCount",
    "LotCountResult",
    "count_lots_by_product",
]
