# -*- coding: utf-8 -*-
"""ledger_io の単体試験。OS 依存なし（tempfile + pathlib のみ）で Linux CI でも走る。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# scripts/ を import パスに入れる（scripts/weekly_scan を package として読む）。
_HERE = Path(__file__).resolve()
_SCRIPTS = _HERE.parents[2]  # .../scripts
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from weekly_scan import ledger_io  # noqa: E402
from weekly_scan.ledger_io import Ledger, LedgerEntry  # noqa: E402


class LedgerEntryTests(unittest.TestCase):
    def test_rejects_empty_analysis_id(self):
        with self.assertRaises(ValueError):
            LedgerEntry(analysis_id="", classification="B")

    def test_rejects_unknown_classification(self):
        with self.assertRaises(ValueError):
            LedgerEntry(analysis_id="x", classification="Z")

    def test_accepts_all_five_classes(self):
        for c in ("A", "B", "C", "D", "E"):
            e = LedgerEntry(analysis_id="id_%s" % c, classification=c)
            self.assertEqual(e.classification, c)

    def test_rejects_negative_lots(self):
        with self.assertRaises(ValueError):
            LedgerEntry(analysis_id="x", classification="C", required_lots=-1)
        with self.assertRaises(ValueError):
            LedgerEntry(analysis_id="x", classification="C", current_lots=-2)

    def test_unknown_fields_are_preserved_in_notes(self):
        e = LedgerEntry.from_dict({
            "analysis_id": "a1",
            "classification": "D",
            "some_future_key": 123,
        })
        self.assertEqual(e.notes["_unknown_fields"]["some_future_key"], 123)


class LedgerTests(unittest.TestCase):
    def test_upsert_replaces_same_id(self):
        led = Ledger()
        led.upsert(LedgerEntry(analysis_id="a", classification="B", reason="first"))
        led.upsert(LedgerEntry(analysis_id="a", classification="C", reason="second"))
        self.assertEqual(len(led.entries), 1)
        self.assertEqual(led.get("a").classification, "C")
        self.assertEqual(led.get("a").reason, "second")

    def test_by_classification_sorted(self):
        led = Ledger()
        led.upsert(LedgerEntry(analysis_id="z", classification="B"))
        led.upsert(LedgerEntry(analysis_id="a", classification="B"))
        led.upsert(LedgerEntry(analysis_id="m", classification="A"))
        got = [e.analysis_id for e in led.by_classification("B")]
        self.assertEqual(got, ["a", "z"])

    def test_rescan_classes_excludes_a_and_e(self):
        # B/C/D は再走査対象、A/E は対象外。「一度 GO でないと判定したものを
        # 取り逸す」を防ぐため、B/C/D が残ることを確かめる。
        from weekly_scan import RESCAN_CLASSES
        self.assertEqual(RESCAN_CLASSES, frozenset({"B", "C", "D"}))
        self.assertNotIn("A", RESCAN_CLASSES)
        self.assertNotIn("E", RESCAN_CLASSES)

    def test_roundtrip_json_file(self):
        led = Ledger()
        led.upsert(LedgerEntry(
            analysis_id="example_k1", classification="C",
            next_trigger="2026-12-01", last_scanned=None,
            reason="concept.md:12 K=3 < k_min", config_id="example_k1",
            required_lots=8, current_lots=3, notes={"owner": "line56"},
        ))
        led.upsert(LedgerEntry(
            analysis_id="example_d4", classification="D",
            reason="re-fetch needed", next_trigger="re-fetch",
        ))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "daicho.json"
            ledger_io.save_ledger(p, led)
            # UTF-8 BOM なし・末尾改行ありを確認。
            raw = p.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertTrue(raw.endswith(b"\n"))
            # 再読込で内容が一致。
            led2 = ledger_io.load_ledger(p)
            self.assertEqual(set(led2.entries), {"example_k1", "example_d4"})
            self.assertEqual(led2.get("example_k1").required_lots, 8)
            self.assertEqual(led2.get("example_k1").notes["owner"], "line56")

    def test_load_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "nope.json"
            led = ledger_io.load_ledger(p)
            self.assertEqual(len(led.entries), 0)

    def test_duplicate_id_in_file_is_rejected(self):
        payload = {
            "schema_version": 1,
            "entries": [
                {"analysis_id": "dup", "classification": "B"},
                {"analysis_id": "dup", "classification": "C"},
            ],
        }
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "daicho.json"
            p.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                ledger_io.load_ledger(p)

    def test_save_is_atomic_leaves_no_tmp(self):
        led = Ledger()
        led.upsert(LedgerEntry(analysis_id="a", classification="B"))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "daicho.json"
            ledger_io.save_ledger(p, led)
            leftovers = [f for f in os.listdir(d) if f != "daicho.json"]
            self.assertEqual(leftovers, [])


class LedgerNoIdentityTests(unittest.TestCase):
    """台帳 I/O は状態だけを書く。社員 ID・認証情報の置き場を持たないことを固定する。"""

    # 台帳スキーマに現れてよいキー。ここに社員 ID・認証情報の欄が足されていないこと。
    _ALLOWED_ENTRY_KEYS = frozenset({
        "analysis_id", "classification", "next_trigger", "last_scanned",
        "reason", "config_id", "required_lots", "current_lots", "notes",
    })

    def test_entry_schema_has_no_identity_or_credential_field(self):
        # dataclass のフィールド集合が許可集合と一致する（新たな欄の混入を検出）。
        fields = set(LedgerEntry.__dataclass_fields__)  # type: ignore[attr-defined]
        self.assertEqual(fields, set(self._ALLOWED_ENTRY_KEYS))
        banned = (
            "employee", "emp_id", "employee_id", "user_id", "userid",
            "password", "passwd", "passphrase", "secret", "token",
            "credential", "api_key", "apikey", "auth",
        )
        for name in fields:
            low = name.lower()
            for bad in banned:
                self.assertNotIn(
                    bad, low, "schema field %r looks like identity/credential" % name
                )

    def test_saved_ledger_bytes_contain_no_credential_markers(self):
        # 通常の状態だけを入れて保存し、書き出したバイト列に認証情報の痕跡が無いこと。
        led = Ledger()
        led.upsert(LedgerEntry(
            analysis_id="example_x1", classification="C",
            reason="concept.md:12 K=3 < k_min", config_id="example_x1",
            required_lots=8, current_lots=3,
        ))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "daicho.json"
            ledger_io.save_ledger(p, led)
            raw = p.read_bytes()
            # BOM 無し・UTF-8 でデコードできること。
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            text = raw.decode("utf-8").lower()
            for marker in ("password", "passphrase", "secret", "token", "api_key", "credential"):
                self.assertNotIn(marker, text)

    def test_credential_like_notes_are_not_invented_by_the_module(self):
        # notes は自由形式だが、モジュールが勝手に認証情報を足さないこと。
        # 既定で空 dict であり、保存→再読込で notes が空のまま保たれる。
        led = Ledger()
        led.upsert(LedgerEntry(analysis_id="a", classification="B"))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "daicho.json"
            ledger_io.save_ledger(p, led)
            led2 = ledger_io.load_ledger(p)
            self.assertEqual(led2.get("a").notes, {})


if __name__ == "__main__":
    unittest.main()
