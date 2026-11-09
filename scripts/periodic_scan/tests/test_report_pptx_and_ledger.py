"""単体試験: pptx 報告生成（python-pptx 無環境ではスキップで緑）と台帳追記。"""

import json
import os

import pytest

from scripts.periodic_scan.report.report_pptx import (
    build_slide_spec,
    pptx_available,
    write_pptx,
)
from scripts.periodic_scan.report.ledger_append import (
    detection_to_entry,
    append_tracking,
)


def _rec():
    return {
        "analysis_id": "screen-指標値-20261104",
        "kind": "strong_candidate",
        "result_item": "指標値",
        "what_we_saw": ["treated が一貫して高い"],
        "whats_missing": ["各群5に未達"],
        "numbers": {"pooled_delta": 0.18, "n_total": 36},
        "proposal": {"change": "c", "experiment": "e", "confirm_design": "d"},
        "tracking": {"next_trigger": "各群5到達", "class": "C"},
    }


def test_build_slide_spec_is_pure_data_without_pptx():
    slides = build_slide_spec(_rec())
    titles = [s["title"] for s in slides]
    assert "改善の提案" in titles
    assert "追跡項目（台帳へ追加）" in titles
    # 数値スライドに入力の数値が入る
    num_slide = [s for s in slides if s["title"].startswith("数値")][0]
    joined = "\n".join(num_slide["body"])
    assert "0.18" in joined
    assert "36" in joined


def test_write_pptx_skips_cleanly_when_unavailable(tmp_path, monkeypatch):
    # python-pptx の有無に関わらず、戻り値の契約を検証する。
    monkeypatch.setattr(
        "scripts.periodic_scan.report.report_pptx.pptx_available", lambda: False
    )
    path, msg = write_pptx(_rec(), str(tmp_path))
    assert path is None
    assert "スキップ" in msg


def test_write_pptx_generates_file_if_available(tmp_path):
    if not pptx_available():
        pytest.skip("python-pptx 未インストール")
    path, msg = write_pptx(_rec(), str(tmp_path))
    assert path is not None and os.path.isfile(path)
    assert msg == "ok"


def test_detection_to_entry_maps_tracking():
    entry = detection_to_entry(_rec(), scanned_on="2026-11-04")
    assert entry == {
        "analysis_id": "screen-指標値-20261104",
        "class": "C",
        "next_trigger": "各群5到達",
        "last_scanned": "2026-11-04",
    }


def test_append_tracking_adds_then_updates(tmp_path):
    ledger = os.path.join(str(tmp_path), "台帳.json")
    r1 = append_tracking(_rec(), ledger, scanned_on="2026-11-04")
    assert r1["action"] == "added" and r1["total"] == 1
    # 同じ analysis_id は更新（重複追記しない）
    rec2 = _rec()
    rec2["tracking"]["next_trigger"] = "更新後"
    r2 = append_tracking(rec2, ledger, scanned_on="2026-11-05")
    assert r2["action"] == "updated" and r2["total"] == 1
    data = json.load(open(ledger, encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["next_trigger"] == "更新後"
    assert data[0]["last_scanned"] == "2026-11-05"


def test_append_tracking_keeps_distinct_ids(tmp_path):
    ledger = os.path.join(str(tmp_path), "台帳.json")
    append_tracking(_rec(), ledger, scanned_on="2026-11-04")
    other = _rec()
    other["analysis_id"] = "screen-別項目-20261104"
    r = append_tracking(other, ledger, scanned_on="2026-11-04")
    assert r["total"] == 2
