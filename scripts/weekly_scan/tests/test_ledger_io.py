# -*- coding: utf-8 -*-
"""Tests for scripts.weekly_scan.ledger_io.

Hermetic: tmp_path only, no network, no DB, no Windows-specific paths.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.weekly_scan import ledger_io as L


def test_valid_classes_are_the_fixed_five():
    assert L.VALID_CLASSES == ("A", "B", "C", "D", "E")
    assert L.PROMISING_CLASSES == ("B", "C", "D")


def test_entry_validate_rejects_unknown_class():
    e = L.LedgerEntry(analysis_id="x", classification="Z")
    with pytest.raises(L.LedgerError):
        e.validate()


def test_entry_validate_rejects_empty_id():
    e = L.LedgerEntry(analysis_id="  ", classification="A")
    with pytest.raises(L.LedgerError):
        e.validate()


def test_is_promising_flags_bcd_only():
    assert L.LedgerEntry("a", "B").is_promising
    assert L.LedgerEntry("a", "C").is_promising
    assert L.LedgerEntry("a", "D").is_promising
    assert not L.LedgerEntry("a", "A").is_promising
    assert not L.LedgerEntry("a", "E").is_promising


def test_from_dict_rejects_unknown_field():
    with pytest.raises(L.LedgerError):
        L.LedgerEntry.from_dict({"analysis_id": "a", "classification": "A", "bogus": 1})


def test_upsert_replaces_same_id():
    led = L.Ledger()
    led.upsert(L.LedgerEntry("a", "C", next_trigger="need 10 more"))
    led.upsert(L.LedgerEntry("a", "B", next_trigger="order pending"))
    assert len(led.entries) == 1
    assert led.get("a").classification == "B"
    assert led.get("a").next_trigger == "order pending"


def test_duplicate_ids_fail_validate():
    led = L.Ledger(entries=[L.LedgerEntry("a", "A"), L.LedgerEntry("a", "B")])
    with pytest.raises(L.LedgerError):
        led.validate()


def test_promising_filters_bcd():
    led = L.Ledger(entries=[
        L.LedgerEntry("a", "A"),
        L.LedgerEntry("b", "B"),
        L.LedgerEntry("c", "C"),
        L.LedgerEntry("d", "D"),
        L.LedgerEntry("e", "E"),
    ])
    ids = sorted(e.analysis_id for e in led.promising())
    assert ids == ["b", "c", "d"]


def test_mark_scanned_sets_timestamp_and_rejects_unknown():
    led = L.Ledger(entries=[L.LedgerEntry("a", "C")])
    led.mark_scanned("a", when="2026-11-04T00:00:00+00:00")
    assert led.get("a").last_scanned == "2026-11-04T00:00:00+00:00"
    with pytest.raises(L.LedgerError):
        led.mark_scanned("nope")


def test_load_missing_file_returns_empty_ledger(tmp_path: Path):
    led = L.load_ledger(tmp_path / "does_not_exist.json")
    assert led.entries == []
    assert led.schema_version == L.SCHEMA_VERSION


def test_corrupt_file_is_loud(tmp_path: Path):
    p = tmp_path / "ledger.json"
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(L.LedgerError):
        L.load_ledger(p)


def test_save_then_load_roundtrip_preserves_unicode(tmp_path: Path):
    p = tmp_path / "\u53f0\u5e33.json"
    led = L.Ledger(entries=[
        L.LedgerEntry(
            analysis_id="mcl_shrink_sonota",
            classification="B",
            next_trigger="\u78ba\u8a8d\u7528\u30ed\u30c3\u30c8\u5f85\u3061",
            last_scanned="2026-11-04T00:00:00+00:00",
            evidence={"ci_std": [0.3355, 0.4734], "file": "w67/judge_result.json"},
            verdict="HOLD",
            source_path="AHA\u672c\u756a\u6e96\u5099/\u5224\u5b9a\u5668\u306e\u5b9f\u7269/w67",
        ),
    ])
    L.save_ledger(p, led)
    # No BOM, trailing newline, UTF-8 readable.
    raw = p.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.endswith(b"\n")
    back = L.load_ledger(p)
    assert back.get("mcl_shrink_sonota").classification == "B"
    assert back.get("mcl_shrink_sonota").evidence["ci_std"] == [0.3355, 0.4734]
    # The on-disk JSON keeps non-ASCII as real characters, not \\uXXXX escapes.
    txt = p.read_text(encoding="utf-8")
    assert "\u78ba\u8a8d\u7528\u30ed\u30c3\u30c8\u5f85\u3061" in txt
    obj = json.loads(txt)
    assert obj["schema_version"] == L.SCHEMA_VERSION


def test_save_rejects_invalid_ledger(tmp_path: Path):
    led = L.Ledger(entries=[L.LedgerEntry("a", "Z")])
    with pytest.raises(L.LedgerError):
        L.save_ledger(tmp_path / "l.json", led)


def test_entries_due_defaults_to_bcd():
    led = L.Ledger(entries=[
        L.LedgerEntry("a", "A"),
        L.LedgerEntry("b", "B"),
        L.LedgerEntry("c", "C"),
    ])
    due = sorted(e.analysis_id for e in L.entries_due(led))
    assert due == ["b", "c"]
    only_c = [e.analysis_id for e in L.entries_due(led, classes=("C",))]
    assert only_c == ["c"]
