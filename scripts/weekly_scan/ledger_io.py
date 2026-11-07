# -*- coding: utf-8 -*-
"""Read and write the weekly-scan ledger (``\u53f0\u5e33.json``).

The ledger is the authoritative list of every analysis the weekly scan knows about,
and WHY each one is not (yet) adopted. One JSON object per analysis.

Classification codes (the "not-adopted reason", fixed set of five):

* ``A`` -- confirmed no effect: interval sits inside the threshold AND lots are
  sufficient. This is the ONLY class parked as out-of-scope.
* ``B`` -- post-hoc, so registration order does not hold (an effect trend is there,
  but the order rule keeps it at HOLD).
* ``C`` -- not enough lots (record how many more are needed).
* ``D`` -- suspected acquisition / input error (limited period, failed join, a
  comparison not aligned by product -- needs re-fetch / redo).
* ``E`` -- undecidable (a structural problem).

B / C / D are the "promising miss" candidates the scan must keep looking at; A alone
is the out-of-scope set. E is recorded but not re-scanned on its own.

This module only does I/O and schema validation. It does NOT decide the class -- the
scan body passes the class in. It never writes to H: or SharePoint; the caller chooses
the path (under the operator's local records folder).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

#: The five allowed not-adopted classes. Fixed; do not extend without an owner decision.
VALID_CLASSES = ("A", "B", "C", "D", "E")

#: Classes that remain "promising misses" and must keep being scanned.
PROMISING_CLASSES = ("B", "C", "D")

#: The ledger schema version. Bump when the on-disk shape changes incompatibly.
SCHEMA_VERSION = 1


class LedgerError(ValueError):
    """A ledger record or file violates the schema. Raised rather than silently
    repaired, because a mis-shaped ledger is exactly how a miss gets lost."""


@dataclass
class LedgerEntry:
    """One analysis in the ledger.

    Attributes:
        analysis_id: Stable id of the analysis (e.g. the claim id ``mcl_shrink_sonota``
            or a folder-derived id like ``w67``). Required and unique within a ledger.
        classification: One of :data:`VALID_CLASSES`.
        next_trigger: Free-text description of what event re-opens this analysis
            (e.g. "confirmation lots reach 886 (expected 2026-12-10)", "re-fetch").
            Empty for class A (parked).
        last_scanned: ISO-8601 date/datetime string of the last time the scan looked
            at this analysis, or None if never scanned.
        evidence: Free-form dict of the basis for the classification (file, line,
            value) so the reason is auditable. Not interpreted here.
        verdict: The registered judge verdict as read (e.g. "HOLD"), copied for
            quick reference; never recomputed here.
        source_path: Where the judge result folder for this analysis lives, so the
            scan can re-read ``judge_result.json`` without guessing.
    """

    analysis_id: str
    classification: str
    next_trigger: str = ""
    last_scanned: Optional[str] = None
    evidence: dict[str, Any] = field(default_factory=dict)
    verdict: Optional[str] = None
    source_path: Optional[str] = None

    def validate(self) -> "LedgerEntry":
        if not self.analysis_id or not str(self.analysis_id).strip():
            raise LedgerError("analysis_id must be a non-empty string")
        if self.classification not in VALID_CLASSES:
            raise LedgerError(
                "classification %r is not one of %s"
                % (self.classification, ", ".join(VALID_CLASSES))
            )
        if not isinstance(self.evidence, dict):
            raise LedgerError("evidence must be a dict")
        return self

    @property
    def is_promising(self) -> bool:
        """True when this entry is a B/C/D miss that must keep being scanned."""
        return self.classification in PROMISING_CLASSES

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LedgerEntry":
        if not isinstance(raw, dict):
            raise LedgerError("a ledger entry must be a JSON object")
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(raw) - known
        if unknown:
            raise LedgerError(
                "unknown field(s) in ledger entry: %s" % ", ".join(sorted(unknown))
            )
        return cls(
            analysis_id=raw.get("analysis_id", ""),
            classification=raw.get("classification", ""),
            next_trigger=raw.get("next_trigger", "") or "",
            last_scanned=raw.get("last_scanned"),
            evidence=raw.get("evidence") or {},
            verdict=raw.get("verdict"),
            source_path=raw.get("source_path"),
        ).validate()


@dataclass
class Ledger:
    """The whole ledger: a schema version plus a list of unique entries."""

    entries: list[LedgerEntry] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def validate(self) -> "Ledger":
        seen: set[str] = set()
        for e in self.entries:
            e.validate()
            if e.analysis_id in seen:
                raise LedgerError(
                    "duplicate analysis_id in ledger: %r" % e.analysis_id
                )
            seen.add(e.analysis_id)
        return self

    def get(self, analysis_id: str) -> Optional[LedgerEntry]:
        for e in self.entries:
            if e.analysis_id == analysis_id:
                return e
        return None

    def upsert(self, entry: LedgerEntry) -> "Ledger":
        """Insert a new entry or replace the existing one with the same id.

        Returning self so callers can chain. The entry is validated first, so a bad
        entry never lands in the ledger.
        """
        entry.validate()
        for i, e in enumerate(self.entries):
            if e.analysis_id == entry.analysis_id:
                self.entries[i] = entry
                return self
        self.entries.append(entry)
        return self

    def promising(self) -> list[LedgerEntry]:
        """Every B/C/D entry -- the misses that must keep being scanned."""
        return [e for e in self.entries if e.is_promising]

    def mark_scanned(self, analysis_id: str, when: Optional[str] = None) -> "Ledger":
        """Record that ``analysis_id`` was scanned at ``when`` (default: now, UTC)."""
        e = self.get(analysis_id)
        if e is None:
            raise LedgerError("cannot mark unknown analysis_id %r as scanned" % analysis_id)
        e.last_scanned = when or _now_iso()
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Ledger":
        if not isinstance(raw, dict):
            raise LedgerError("the ledger file must contain a JSON object")
        version = raw.get("schema_version", SCHEMA_VERSION)
        if not isinstance(version, int):
            raise LedgerError("schema_version must be an integer")
        entries_raw = raw.get("entries", [])
        if not isinstance(entries_raw, list):
            raise LedgerError("entries must be a JSON array")
        entries = [LedgerEntry.from_dict(r) for r in entries_raw]
        return cls(entries=entries, schema_version=version).validate()


def _now_iso() -> str:
    """Current time as an ISO-8601 UTC string. Kept as one seam so tests can patch it."""
    return datetime.now(timezone.utc).isoformat()


def load_ledger(path: str | os.PathLike[str]) -> Ledger:
    """Read a ledger JSON file. A missing file yields an empty ledger (not an error),
    because the first scan has nothing to read yet.

    A file that exists but is malformed IS an error -- a corrupt ledger must be loud,
    not silently replaced with an empty one that drops every tracked miss.
    """
    p = Path(path)
    if not p.exists():
        return Ledger()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LedgerError("ledger %s is not valid JSON: %s" % (p, exc)) from exc
    return Ledger.from_dict(raw)


def save_ledger(path: str | os.PathLike[str], ledger: Ledger) -> None:
    """Write the ledger atomically (temp file + replace) so a crash mid-write never
    leaves a half-written ledger that would read as a corrupt file next time.

    UTF-8, no BOM, trailing newline, 2-space indent -- matches the repo's JSON style.
    """
    ledger.validate()
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(ledger.to_dict(), ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def entries_due(
    ledger: Ledger,
    classes: Iterable[str] = PROMISING_CLASSES,
) -> list[LedgerEntry]:
    """Return the entries in the given classes (default B/C/D).

    This is the list the scan body iterates: the misses that must not be dropped.
    """
    wanted = set(classes)
    return [e for e in ledger.entries if e.classification in wanted]
