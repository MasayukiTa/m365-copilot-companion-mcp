"""処理 (d) — 検出結果の追跡項目を台帳.json に追加する。

DetectionRecord.tracking を LedgerEntry（SCHEMA 8）に変換し、指定した台帳.json
（解析ID・分類・次の契機・最後に走査した日 のエントリ配列）へ追記する。

台帳の既存エントリは読み取り、同じ analysis_id は重複追記しない（既存を更新する）。
共通基盤 periodic_scan.ledger_io が揃えばそれを使ってよいが、本モジュールは
無くても単独で動くよう最小の JSON I/O を内蔵する。
"""

from __future__ import annotations

import json
import os
from datetime import date as _date
from typing import Any, Dict, List, Optional


def detection_to_entry(
    rec: Dict[str, Any], scanned_on: Optional[str] = None
) -> Dict[str, Any]:
    """DetectionRecord -> LedgerEntry。"""
    tracking: Dict[str, Any] = rec.get("tracking", {}) or {}
    day = scanned_on or _date.today().isoformat()
    return {
        "analysis_id": rec.get("analysis_id", ""),
        "class": tracking.get("class", ""),
        "next_trigger": tracking.get("next_trigger", ""),
        "last_scanned": day,
    }


def _load_ledger(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError:
            return []
    if isinstance(data, dict) and "entries" in data:
        data = data["entries"]
    return data if isinstance(data, list) else []


def append_tracking(
    rec: Dict[str, Any], ledger_path: str, scanned_on: Optional[str] = None
) -> Dict[str, Any]:
    """DetectionRecord.tracking を台帳.json へ追記/更新し、結果を返す。

    戻り値: {"entry": LedgerEntry, "action": "added"|"updated", "total": int}
    """
    entry = detection_to_entry(rec, scanned_on=scanned_on)
    entries = _load_ledger(ledger_path)
    action = "added"
    aid = entry["analysis_id"]
    found = False
    for i, e in enumerate(entries):
        if isinstance(e, dict) and e.get("analysis_id") == aid and aid:
            entries[i] = entry
            action = "updated"
            found = True
            break
    if not found:
        entries.append(entry)
    os.makedirs(os.path.dirname(os.path.abspath(ledger_path)) or ".", exist_ok=True)
    with open(ledger_path, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, ensure_ascii=False, indent=2)
    return {"entry": entry, "action": action, "total": len(entries)}


__all__ = ["detection_to_entry", "append_tracking"]
