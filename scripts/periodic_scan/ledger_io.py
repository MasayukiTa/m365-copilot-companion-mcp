# -*- coding: utf-8 -*-
"""Backlog ledger read/write I/O.

The ledger is the central state file for the periodic scan. One entry = one
analysis, with this schema:

    {
      "analysis_id":    str,          # analysis id (e.g. "A0001"). Unique.
      "classification": "A"|"B"|"C"|"D"|"E",  # the 5-way triage class
      "next_trigger":   str|null,     # what will prompt the next look
                                      #   (e.g. a date, "re-acquire", or null)
      "last_scanned":   str|null,     # last scan date (ISO 8601; null if never)
      "reason":         str,          # basis for the class (file/line/value)
      "config_id":      str|null,     # id of the registered claim for the judge
      "required_lots":  int|null,     # lots needed for a class C analysis
      "current_lots":   int|null,     # most recent counted lot total
      "notes":          dict          # free-form follow-up tracking
    }

The file is written UTF-8 (no BOM), stable key order, 2-space indent, with a
trailing newline, and atomically (temp file in the same directory, then
os.replace) so an interrupted write never leaves a half-written ledger.

This module reads/writes state only. It never touches the judge, criteria,
effect-size floor or any correction, and holds no connection string, credential
or identifier for any real system.
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
    """One ledger entry."""

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
        # Preserve unknown keys under notes (forward compatibility).
        if extra:
            merged = dict(entry.notes)
            merged.setdefault("_unknown_fields", {}).update(extra)
            entry.notes = merged
        return entry


@dataclass
class Ledger:
    """The whole ledger, keyed uniquely by analysis_id."""

    entries: dict[str, LedgerEntry] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def upsert(self, entry: LedgerEntry) -> None:
        """Insert or replace by analysis_id."""
        self.entries[entry.analysis_id] = entry

    def get(self, analysis_id: str) -> LedgerEntry | None:
        return self.entries.get(analysis_id)

    def remove(self, analysis_id: str) -> bool:
        return self.entries.pop(analysis_id, None) is not None

    def by_classification(self, *classes: str) -> list[LedgerEntry]:
        """Entries of the given classes, in analysis_id order."""
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
    """Read the ledger JSON. Returns an empty ledger if the file is absent."""
    p = Path(path)
    if not p.exists():
        return Ledger()
    text = p.read_text(encoding="utf-8")
    if not text.strip():
        return Ledger()
    return Ledger.from_dict(json.loads(text))


def save_ledger(path: str | os.PathLike[str], ledger: Ledger) -> None:
    """Write the ledger JSON atomically (UTF-8, no BOM, 2-space, trailing \\n)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        ledger.to_dict(), ensure_ascii=False, indent=2, sort_keys=False
    )
    if not payload.endswith("\n"):
        payload += "\n"
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
