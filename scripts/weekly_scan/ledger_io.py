"""台帳 台帳.json の読み書き I/O。

台帳は週次走査の中心となる状態ファイル。1 エントリ = 1 解析で、次のスキーマを持つ:

    {
      "analysis_id":  str,          # 解析 ID（例 "example_doushiwa_analysis"）。一意。
      "classification": "A"|"B"|"C"|"D"|"E",  # 棚卸しの5分類
      "next_trigger":  str|null,    # 次の契機（例 "確認用ロットが規定数に達する予定日: 2026-12-01",
                                    #   "再取得"、null=契機なし/対象外）
      "last_scanned":  str|null,    # 最後に走査した日（ISO 8601、未走査は null）
      "reason":        str,         # 分類の根拠（ファイル・行・値）
      "config_id":     str|null,    # 判定器に渡す config_id（既存の登録済み主張の ID）
      "required_lots": int|null,    # C 分類で必要なロット数
      "current_lots":  int|null,    # 直近に数えたロット数
      "notes":         dict         # 追加の追跡項目（自由形式。報告・提案の追跡に使う）
    }

ファイルは UTF-8（BOM なし）、キー順安定、2 スペースインデントで書く。原子的に書く
（同じディレクトリに一時ファイルを作ってから os.replace）ので、途中で中断しても
半分書けた台帳を読むことはない。

このモジュールは判定器・基準・Δmin・補正を一切参照しない。状態の読み書きだけを担う。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

from . import VALID_CLASSES

SCHEMA_VERSION = 1


@dataclass
class LedgerEntry:
    """台帳の 1 エントリ。"""

    analysis_id: str
    classification: str
    next_trigger: str | None = None
    last_scanned: str | None = None
    reason: str = ""
    config_id: str | None = None
    required_lots: int | None = None
    current_lots: int | None = None
    notes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.analysis_id or not str(self.analysis_id).strip():
            raise ValueError("analysis_id must be a non-empty string")
        if self.classification not in VALID_CLASSES:
            raise ValueError(
                "classification must be one of %s, got %r"
                % (sorted(VALID_CLASSES), self.classification)
            )
        if self.required_lots is not None and self.required_lots < 0:
            raise ValueError("required_lots must be >= 0 when set")
        if self.current_lots is not None and self.current_lots < 0:
            raise ValueError("current_lots must be >= 0 when set")
        if not isinstance(self.notes, dict):
            raise ValueError("notes must be a dict")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LedgerEntry":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in d.items() if k in known}
        extra = {k: v for k, v in d.items() if k not in known}
        entry = cls(**kwargs)
        # 未知のキーは notes に退避して失わない（前方互換）。
        if extra:
            merged = dict(entry.notes)
            merged.setdefault("_unknown_fields", {}).update(extra)
            entry.notes = merged
        return entry


@dataclass
class Ledger:
    """台帳全体。analysis_id をキーに一意化して保持する。"""

    entries: dict[str, LedgerEntry] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def upsert(self, entry: LedgerEntry) -> None:
        """エントリを追加または置換する（analysis_id が同じなら上書き）。"""
        self.entries[entry.analysis_id] = entry

    def get(self, analysis_id: str) -> LedgerEntry | None:
        return self.entries.get(analysis_id)

    def remove(self, analysis_id: str) -> bool:
        return self.entries.pop(analysis_id, None) is not None

    def by_classification(self, *classes: str) -> list[LedgerEntry]:
        """指定した分類のエントリを analysis_id 昇順で返す。"""
        want = set(classes)
        return [
            self.entries[k]
            for k in sorted(self.entries)
            if self.entries[k].classification in want
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "entries": [
                self.entries[k].to_dict() for k in sorted(self.entries)
            ],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Ledger":
        raw_entries: Iterable[dict[str, Any]] = d.get("entries", []) or []
        led = cls(schema_version=int(d.get("schema_version", SCHEMA_VERSION)))
        for row in raw_entries:
            entry = LedgerEntry.from_dict(row)
            if entry.analysis_id in led.entries:
                raise ValueError(
                    "duplicate analysis_id in ledger: %r" % entry.analysis_id
                )
            led.entries[entry.analysis_id] = entry
        return led


def load_ledger(path: str | os.PathLike[str]) -> Ledger:
    """台帳.json を読む。ファイルが無ければ空の台帳を返す。"""
    p = Path(path)
    if not p.exists():
        return Ledger()
    text = p.read_text(encoding="utf-8")
    if not text.strip():
        return Ledger()
    return Ledger.from_dict(json.loads(text))


def save_ledger(path: str | os.PathLike[str], ledger: Ledger) -> None:
    """台帳.json を原子的に書く（UTF-8、BOM なし、2 スペース、末尾改行あり）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        ledger.to_dict(), ensure_ascii=False, indent=2, sort_keys=False
    )
    if not payload.endswith("\n"):
        payload += "\n"
    # 同じディレクトリに一時ファイルを作り、flush+fsync してから置換する。
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(p))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


__all__ = [
    "SCHEMA_VERSION",
    "LedgerEntry",
    "Ledger",
    "load_ledger",
    "save_ledger",
]
