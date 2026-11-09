# -*- coding: utf-8 -*-
"""Unit tests for db_lots. The odbc_query tool is never called; a fake
query_callable is injected. No real connection/table/column names appear -- the
connection name is supplied by the test (explicitly or via a synthetic profile).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve()
_SCRIPTS = _HERE.parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from periodic_scan.db_lots import (  # noqa: E402
    LotCountResult,
    count_lots_by_product,
    default_connection,
)

# Synthetic identifiers used throughout (nothing site-specific).
CONN = "conn1"
TABLE = "PROD.dbo.V_metric"
UNIT = "lot"
PRODUCT = "product"


class _Recorder:
    """Fake query_callable. Records the args it was given and returns fixed rows."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def __call__(self, connection, query, params=None):
        self.calls.append({"connection": connection, "query": query, "params": list(params or [])})
        return self.rows


class DefaultConnectionTests(unittest.TestCase):
    def test_profile_supplies_connection_name(self):
        self.assertEqual(default_connection({"connection": "conn1"}), "conn1")

    def test_absent_connection_is_none_not_hardcoded(self):
        # With no profile value the module invents nothing: it returns None and
        # the caller must pass connection= explicitly.
        self.assertIsNone(default_connection({}))


class CountLotsTests(unittest.TestCase):
    def test_groups_and_sorts_by_product(self):
        rec = _Recorder([("P_B", 5), ("P_A", 3)])
        res = count_lots_by_product(
            rec, table=TABLE, unit_column=UNIT, product_column=PRODUCT, connection=CONN,
        )
        self.assertIsInstance(res, LotCountResult)
        self.assertEqual([c.product for c in res.per_product], ["P_A", "P_B"])
        self.assertEqual(res.total_lots, 8)
        self.assertEqual(res.get("P_A"), 3)

    def test_uses_count_distinct_and_group_by(self):
        rec = _Recorder([("P_A", 3)])
        count_lots_by_product(rec, "T", "lot", "product", connection=CONN)
        q = rec.calls[0]["query"]
        self.assertIn("COUNT(DISTINCT lot)", q)
        self.assertIn("GROUP BY product", q)

    def test_product_filter_is_bound_not_inlined(self):
        rec = _Recorder([("P_A", 3)])
        count_lots_by_product(
            rec, "T", "lot", "product", products=["P_A", "P_B"], connection=CONN,
        )
        call = rec.calls[0]
        self.assertIn("IN (?, ?)", call["query"])
        self.assertEqual(call["params"], ["P_A", "P_B"])
        self.assertNotIn("P_A", call["query"])

    def test_where_clause_params_precede_product_params(self):
        rec = _Recorder([("P_A", 3)])
        count_lots_by_product(
            rec, "T", "lot", "product",
            products=["P_A"],
            where_sql="ymd >= ?", where_params=["2026-08-01"], connection=CONN,
        )
        self.assertEqual(rec.calls[0]["params"], ["2026-08-01", "P_A"])

    def test_connection_name_is_passed_through(self):
        rec = _Recorder([("P_A", 3)])
        count_lots_by_product(rec, "T", "lot", "product", connection=CONN)
        self.assertEqual(rec.calls[0]["connection"], CONN)

    def test_missing_connection_raises(self):
        rec = _Recorder([])
        with self.assertRaises(ValueError):
            # No connection= and no profile value -> refuse to invent one.
            count_lots_by_product(rec, "T", "lot", "product", connection=None)

    def test_meets_required_total_and_per_product(self):
        rec = _Recorder([("P_A", 3), ("P_B", 5)])
        res = count_lots_by_product(rec, "T", "lot", "product", connection=CONN)
        self.assertTrue(res.meets_required(8))
        self.assertFalse(res.meets_required(9))
        self.assertTrue(res.meets_required(3, product="P_A"))
        self.assertFalse(res.meets_required(4, product="P_A"))

    def test_rejects_unsafe_table_identifier(self):
        rec = _Recorder([])
        with self.assertRaises(ValueError):
            count_lots_by_product(rec, "T; DROP TABLE x", "lot", "product", connection=CONN)

    def test_rejects_unsafe_product_value(self):
        rec = _Recorder([])
        with self.assertRaises(ValueError):
            count_lots_by_product(rec, "T", "lot", "product", products=["ok", "bad'val"], connection=CONN)

    def test_dict_rows_are_parsed(self):
        rec = _Recorder([{"product": "P_A", "lots": 7}])
        res = count_lots_by_product(rec, "T", "lot", "product", connection=CONN)
        self.assertEqual(res.get("P_A"), 7)

    def test_custom_result_parser(self):
        rec = _Recorder("P_A,3\nP_B,5")

        def parser(raw):
            out = []
            for line in raw.splitlines():
                prod, n = line.split(",")
                out.append((prod, int(n)))
            return out

        res = count_lots_by_product(rec, "T", "lot", "product", result_parser=parser, connection=CONN)
        self.assertEqual(res.total_lots, 8)


if __name__ == "__main__":
    unittest.main()
