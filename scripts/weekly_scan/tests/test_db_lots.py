# -*- coding: utf-8 -*-
"""db_lots の単体試験。odbc_query 本体は呼ばず、ダミーの query_callable を差す。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve()
_SCRIPTS = _HERE.parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from weekly_scan.db_lots import (  # noqa: E402
    DEFAULT_CONNECTION,
    LotCountResult,
    count_lots_by_product,
)


class _Recorder:
    """query_callable のダミー。渡された引数を記録し、固定の行を返す。"""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def __call__(self, connection, query, params=None):
        self.calls.append({"connection": connection, "query": query, "params": list(params or [])})
        return self.rows


class CountLotsTests(unittest.TestCase):
    def test_default_connection_is_adams(self):
        self.assertEqual(DEFAULT_CONNECTION, "adams")

    def test_groups_and_sorts_by_product(self):
        rec = _Recorder([("P_B", 5), ("P_A", 3)])
        res = count_lots_by_product(
            rec, table="MCL.dbo.V_furyo", unit_column="lot", product_column="hinmei",
        )
        self.assertIsInstance(res, LotCountResult)
        self.assertEqual([c.product for c in res.per_product], ["P_A", "P_B"])
        self.assertEqual(res.total_lots, 8)
        self.assertEqual(res.get("P_A"), 3)

    def test_uses_count_distinct_and_group_by(self):
        rec = _Recorder([("P_A", 3)])
        count_lots_by_product(rec, "T", "lot", "hinmei")
        q = rec.calls[0]["query"]
        self.assertIn("COUNT(DISTINCT lot)", q)
        self.assertIn("GROUP BY hinmei", q)

    def test_product_filter_is_bound_not_inlined(self):
        rec = _Recorder([("P_A", 3)])
        count_lots_by_product(
            rec, "T", "lot", "hinmei", products=["P_A", "P_B"],
        )
        call = rec.calls[0]
        # 値は params に入り、SQL には ? が並ぶ（値を文字列に埋め込まない）。
        self.assertIn("IN (?, ?)", call["query"])
        self.assertEqual(call["params"], ["P_A", "P_B"])
        self.assertNotIn("P_A", call["query"])

    def test_where_clause_params_precede_product_params(self):
        rec = _Recorder([("P_A", 3)])
        count_lots_by_product(
            rec, "T", "lot", "hinmei",
            products=["P_A"],
            where_sql="ymd >= ?", where_params=["2026-08-01"],
        )
        self.assertEqual(rec.calls[0]["params"], ["2026-08-01", "P_A"])

    def test_meets_required_total_and_per_product(self):
        rec = _Recorder([("P_A", 3), ("P_B", 5)])
        res = count_lots_by_product(rec, "T", "lot", "hinmei")
        self.assertTrue(res.meets_required(8))          # 合計 8 >= 8
        self.assertFalse(res.meets_required(9))
        self.assertTrue(res.meets_required(3, product="P_A"))
        self.assertFalse(res.meets_required(4, product="P_A"))

    def test_rejects_unsafe_table_identifier(self):
        rec = _Recorder([])
        with self.assertRaises(ValueError):
            count_lots_by_product(rec, "T; DROP TABLE x", "lot", "hinmei")

    def test_rejects_unsafe_product_value(self):
        rec = _Recorder([])
        with self.assertRaises(ValueError):
            count_lots_by_product(rec, "T", "lot", "hinmei", products=["ok", "bad'val"])

    def test_dict_rows_are_parsed(self):
        rec = _Recorder([{"product": "P_A", "lots": 7}])
        res = count_lots_by_product(rec, "T", "lot", "hinmei")
        self.assertEqual(res.get("P_A"), 7)

    def test_custom_result_parser(self):
        rec = _Recorder("P_A,3\nP_B,5")

        def parser(raw):
            out = []
            for line in raw.splitlines():
                prod, n = line.split(",")
                out.append((prod, int(n)))
            return out

        res = count_lots_by_product(rec, "T", "lot", "hinmei", result_parser=parser)
        self.assertEqual(res.total_lots, 8)


if __name__ == "__main__":
    unittest.main()
