"""台帳 I/O(ledger_io)の単体試験。Linux の CI で走る(Windows 専用記述なし)。"""

from __future__ import annotations

import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts.weekly_scan import ledger_io  # noqa: E402
from scripts.weekly_scan.ledger_io import (  # noqa: E402
    Ledger,
    LedgerEntry,
    load_ledger,
    mark_scanned,
    save_ledger,
)


def test_entry_requires_valid_classification():
    with pytest.raises(ValueError):
        LedgerEntry(analysis_id="x", classification="Z")
    with pytest.raises(ValueError):
        LedgerEntry(analysis_id="", classification="A")


def test_candidate_flag_only_bcd():
    assert LedgerEntry("a", "A").is_candidate is False
    assert LedgerEntry("b", "B").is_candidate is True
    assert LedgerEntry("c", "C").is_candidate is True
    assert LedgerEntry("d", "D").is_candidate is True
    assert LedgerEntry("e", "E").is_candidate is False


def test_candidates_filters_bcd_only():
    led = Ledger(
        entries=[
            LedgerEntry("a1", "A"),
            LedgerEntry("b1", "B"),
            LedgerEntry("c1", "C"),
            LedgerEntry("d1", "D"),
            LedgerEntry("e1", "E"),
        ]
    )
    ids = sorted(e.analysis_id for e in led.candidates())
    assert ids == ["b1", "c1", "d1"]


def test_upsert_replaces_same_id():
    led = Ledger(entries=[LedgerEntry("x", "C", next_trigger="old")])
    led.upsert(LedgerEntry("x", "B", next_trigger="new"))
    assert len(led.entries) == 1
    assert led.by_id("x").classification == "B"
    assert led.by_id("x").next_trigger == "new"
    led.upsert(LedgerEntry("y", "A"))
    assert len(led.entries) == 2


def test_roundtrip_save_load_preserves_japanese(tmp_path):
    path = os.path.join(str(tmp_path), "台帳.json")
    led = Ledger(
        entries=[
            LedgerEntry(
                analysis_id="mcl_shrink_sonota",
                classification="B",
                next_trigger="確認用ロットが規定数に達する予定日: 未定",
                last_scanned="",
                evidence="概要.md 26行 区間[0.3355,0.4734] Δmin0.2",
                verdict="HOLD",
                reasons=["D_FAIL_NO_LOCK", "D_FAIL_EXPLORATORY"],
                notes="効果は大きいが登録順序で保留",
            )
        ]
    )
    save_ledger(path, led)
    assert os.path.exists(path)
    # 日本語が UTF-8 で保存され、ASCII エスケープされていないこと。
    raw = open(path, encoding="utf-8").read()
    assert "確認用ロット" in raw
    back = load_ledger(path)
    assert back.schema_version == led.schema_version
    e = back.by_id("mcl_shrink_sonota")
    assert e is not None
    assert e.classification == "B"
    assert e.reasons == ["D_FAIL_NO_LOCK", "D_FAIL_EXPLORATORY"]
    assert e.next_trigger.startswith("確認用ロット")


def test_load_missing_returns_empty(tmp_path):
    path = os.path.join(str(tmp_path), "no_such.json")
    led = load_ledger(path)
    assert led.entries == []


def test_duplicate_ids_rejected_on_load(tmp_path):
    path = os.path.join(str(tmp_path), "dup.json")
    payload = {
        "schema_version": "1.0",
        "entries": [
            {"analysis_id": "x", "classification": "B"},
            {"analysis_id": "x", "classification": "C"},
        ],
    }
    open(path, "w", encoding="utf-8").write(json.dumps(payload, ensure_ascii=False))
    with pytest.raises(ValueError):
        load_ledger(path)


def test_save_is_atomic_leaves_no_tmp(tmp_path):
    path = os.path.join(str(tmp_path), "台帳.json")
    save_ledger(path, Ledger(entries=[LedgerEntry("a", "A")]))
    leftovers = [n for n in os.listdir(str(tmp_path)) if n.endswith(".tmp")]
    assert leftovers == []


def test_mark_scanned_sets_date_only_when_found():
    led = Ledger(entries=[LedgerEntry("a", "B")])
    assert mark_scanned(led, "a", "2026-11-04") is True
    assert led.by_id("a").last_scanned == "2026-11-04"
    assert mark_scanned(led, "missing", "2026-11-04") is False


def test_from_dict_ignores_unknown_fields():
    e = LedgerEntry.from_dict(
        {"analysis_id": "a", "classification": "C", "unknown": 1, "notes": "n"}
    )
    assert e.analysis_id == "a"
    assert e.notes == "n"


def test_constants_exposed():
    assert ledger_io.CANDIDATE_CLASSIFICATIONS == ("B", "C", "D")
    assert set(ledger_io.VALID_CLASSIFICATIONS) == {"A", "B", "C", "D", "E"}
