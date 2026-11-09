# -*- coding: utf-8 -*-
"""runner（共通基盤の結線層）の単体試験。DB も判定器もダミーを差す。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve()
_SCRIPTS = _HERE.parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from weekly_scan.ledger_io import Ledger, LedgerEntry, load_ledger, save_ledger  # noqa: E402
from weekly_scan.runner import (  # noqa: E402
    run_once,
    scan_entry,
    scan_ledger,
)


# 判定器の返値を模す（ネスト verdict を持つ judge_result.json の形）。
_VERDICT_SAMPLE = {
    "claim_id": "example_b1",
    "verdict": {
        "id": "example_b1",
        "verdict": "HOLD",
        "finding": "ORDER_NOT_ESTABLISHED",
        "evidence": {"K": 8},
    },
    "order": {"order_ok": False},
}


def _fake_judge(config_id, engine_version=""):
    return {**_VERDICT_SAMPLE, "claim_id": config_id,
            "verdict": {**_VERDICT_SAMPLE["verdict"], "id": config_id}}


def _fake_query_rows(rows):
    """[(product, lots)] を返す query_callable を作る。"""
    def _q(connection, query, params):
        return list(rows)
    return _q


class ScanEntryTests(unittest.TestCase):
    def test_b_entry_is_judged_without_lot_count(self):
        entry = LedgerEntry(
            analysis_id="a_b", classification="B", config_id="example_b1"
        )
        out = scan_entry(entry, judge_callable=_fake_judge, scanned_at="2026-11-04")
        self.assertTrue(out.judged)
        self.assertEqual(out.verdict, "HOLD")
        self.assertEqual(entry.last_scanned, "2026-11-04")

    def test_c_entry_counts_lots_and_judges_when_met(self):
        entry = LedgerEntry(
            analysis_id="a_c", classification="C", config_id="example_c1",
            required_lots=10,
            notes={"lot_count": {"table": "MCL.dbo.V", "unit_column": "lot",
                                 "product_column": "hinmei"}},
        )
        q = _fake_query_rows([("P1", 7), ("P2", 5)])  # 合計 12 >= 10
        out = scan_entry(entry, query_callable=q, judge_callable=_fake_judge,
                         scanned_at="2026-11-04")
        self.assertTrue(out.lots_checked)
        self.assertEqual(out.current_lots, 12)
        self.assertTrue(out.lots_met)
        self.assertEqual(entry.current_lots, 12)
        self.assertTrue(out.judged)

    def test_c_entry_not_judged_when_lots_short(self):
        entry = LedgerEntry(
            analysis_id="a_c2", classification="C", config_id="example_c2",
            required_lots=20,
            notes={"lot_count": {"table": "MCL.dbo.V", "unit_column": "lot",
                                 "product_column": "hinmei"}},
        )
        q = _fake_query_rows([("P1", 3)])  # 3 < 20
        out = scan_entry(entry, query_callable=q, judge_callable=_fake_judge,
                         scanned_at="2026-11-04")
        self.assertTrue(out.lots_checked)
        self.assertEqual(out.current_lots, 3)
        self.assertFalse(out.lots_met)
        self.assertFalse(out.judged)

    def test_c_entry_without_lotcount_spec_is_not_counted(self):
        entry = LedgerEntry(
            analysis_id="a_c3", classification="C", config_id="example_c3",
            required_lots=5,
        )
        q = _fake_query_rows([("P1", 9)])
        out = scan_entry(entry, query_callable=q, judge_callable=_fake_judge)
        self.assertFalse(out.lots_checked)
        self.assertIsNone(out.current_lots)

    def test_dry_run_entry_only_updates_last_scanned(self):
        entry = LedgerEntry(analysis_id="a_d", classification="D",
                            config_id="example_d1")
        out = scan_entry(entry, scanned_at="2026-11-04")
        self.assertFalse(out.judged)
        self.assertEqual(entry.last_scanned, "2026-11-04")


class ScanLedgerTests(unittest.TestCase):
    def _ledger(self):
        led = Ledger()
        led.upsert(LedgerEntry(analysis_id="keep_a", classification="A"))
        led.upsert(LedgerEntry(analysis_id="keep_e", classification="E"))
        led.upsert(LedgerEntry(analysis_id="do_b", classification="B",
                               config_id="example_b1"))
        led.upsert(LedgerEntry(analysis_id="do_d", classification="D"))
        return led

    def test_only_bcd_are_scanned(self):
        led = self._ledger()
        report = scan_ledger(led, judge_callable=_fake_judge,
                             scanned_at="2026-11-04")
        ids = {o.analysis_id for o in report.outcomes}
        self.assertEqual(ids, {"do_b", "do_d"})
        # A/E は触らない。
        self.assertIsNone(led.get("keep_a").last_scanned)
        self.assertIsNone(led.get("keep_e").last_scanned)

    def test_run_once_saves_ledger(self):
        led = self._ledger()
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "台帳.json")
            save_ledger(path, led)
            report = run_once(path, judge_callable=_fake_judge,
                              scanned_at="2026-11-04")
            self.assertEqual(len(report.outcomes), 2)
            reread = load_ledger(path)
            self.assertEqual(reread.get("do_b").last_scanned, "2026-11-04")
            self.assertEqual(reread.get("do_d").last_scanned, "2026-11-04")
            self.assertIsNone(reread.get("keep_a").last_scanned)


if __name__ == "__main__":
    unittest.main()
