# -*- coding: utf-8 -*-
"""Tests for scripts.weekly_scan.db_lots.

Hermetic: a fake query_fn stands in for tools.odbc_ops.odbc_query, so no pyodbc, no
DB, no network. No Windows-specific paths.
"""

from __future__ import annotations

import pytest

from scripts.weekly_scan import db_lots as D


class _FakeQuery:
    """Records the SQL/params it was called with and returns a canned text table."""

    def __init__(self, table_text: str):
        self.table_text = table_text
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, connection, query, params=None, **kwargs):
        self.calls.append((connection, query, params))
        return self.table_text


ALLOWED = (
    "product         lots\n"
    "E770G           2278\n"
    "OTHER           16551\n"
    "--- 2 row(s)\n"
)


def test_count_lots_by_product_parses_strata():
    q = _FakeQuery(ALLOWED)
    counts = D.count_lots_by_product(
        "ADAMS", table="shrink", lot_column="lotno", product_column="hinmei", query_fn=q
    )
    got = {c.product: c.lots for c in counts}
    assert got == {"E770G": 2278, "OTHER": 16551}


def test_count_builds_distinct_groupby_sql():
    q = _FakeQuery(ALLOWED)
    D.count_lots_by_product(
        "ADAMS", table="shrink", lot_column="lotno", product_column="hinmei", query_fn=q
    )
    _, sql, _ = q.calls[0]
    assert "COUNT(DISTINCT lotno)" in sql
    assert "GROUP BY hinmei" in sql
    assert "FROM shrink" in sql


def test_where_params_are_passed_not_spliced():
    q = _FakeQuery(ALLOWED)
    D.count_lots_by_product(
        "ADAMS", table="shrink", lot_column="lotno", product_column="hinmei",
        where="date >= ?", params=["2026-01-01"], query_fn=q,
    )
    _, sql, params = q.calls[0]
    assert "WHERE date >= ?" in sql
    assert params == ["2026-01-01"]


@pytest.mark.parametrize("bad", ["a; DROP TABLE t", "a b", "", "a--", "a)", "a'"])
def test_bad_identifiers_are_refused(bad):
    q = _FakeQuery(ALLOWED)
    with pytest.raises(D.DbLotsError):
        D.count_lots_by_product("ADAMS", table=bad, lot_column="l", product_column="p", query_fn=q)
    with pytest.raises(D.DbLotsError):
        D.count_lots_by_product("ADAMS", table="t", lot_column=bad, product_column="p", query_fn=q)
    with pytest.raises(D.DbLotsError):
        D.count_lots_by_product("ADAMS", table="t", lot_column="l", product_column=bad, query_fn=q)


def test_schema_qualified_and_japanese_identifiers_allowed():
    q = _FakeQuery(ALLOWED)
    # schema.table and a Japanese column name must both pass the identifier gate.
    D.count_lots_by_product(
        "ADAMS", table="dbo.shrink", lot_column="\u30ed\u30c3\u30c8NO",
        product_column="\u54c1\u540d", query_fn=q,
    )
    _, sql, _ = q.calls[0]
    assert "FROM dbo.shrink" in sql


def test_db_error_is_raised_not_swallowed():
    q = _FakeQuery("[odbc_query error: ProgrammingError: invalid object name]")
    with pytest.raises(D.DbLotsError):
        D.count_lots_by_product("ADAMS", table="t", lot_column="l", product_column="p", query_fn=q)


def test_lots_for_product_and_shortfall():
    counts = [D.LotCount("E770G", 2278), D.LotCount("OTHER", 16551)]
    assert D.lots_for_product(counts, "E770G") == 2278
    assert D.lots_for_product(counts, "MISSING") == 0
    # needs 886, has 2278 -> no shortfall
    assert D.lots_shortfall(counts, "E770G", 886) == 0
    # a product with only 10 lots needs 876 more to reach 886
    small = [D.LotCount("NEW", 10)]
    assert D.lots_shortfall(small, "NEW", 886) == 876
    assert D.lots_shortfall(small, "ABSENT", 886) == 886


def test_shortfall_rejects_negative_required():
    with pytest.raises(D.DbLotsError):
        D.lots_shortfall([], "x", -1)


def test_product_reached_requirement():
    counts = [D.LotCount("E770G", 2278)]
    assert D.product_reached_requirement(counts, "E770G", 886) is True
    assert D.product_reached_requirement(counts, "E770G", 3000) is False
    assert D.product_reached_requirement(counts, "OTHER", 1) is False


def test_numeric_product_id_matches_by_string():
    counts = [D.LotCount(1, 50)]
    assert D.lots_for_product(counts, 1) == 50
    assert D.lots_for_product(counts, "1") == 50
