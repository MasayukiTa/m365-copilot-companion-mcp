"""Common scaffolding for the weekly scan B/C and D drivers.

Subtask 2 (the shared foundation module) may not be merged yet. To stay
independently buildable and testable, this module DEFINES the contracts that
the B/C and D drivers depend on, as explicit function signatures / Protocols.
When subtask 2 lands, these Protocols are the seam its concrete implementations
plug into; nothing here needs to change.

Hard rules honoured by this package:
  * The judge, its thresholds, Delta_min and corrections are NEVER altered. The
    judge is an injected callable; we only pass data in and read the verdict.
  * The order (trusted-clock) record is taken from the ALREADY-REGISTERED claim.
    We never forge, re-stamp, or back-date a lock. If a registered lock is
    absent, order simply stays not-established (max HOLD) — exactly as the judge
    already decides.
  * "Never drop a non-GO analysis from scanning": only class A is out of scope.
    B, C, D stay scannable every run. This module provides the ledger plumbing;
    the drivers decide what to do per class.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Protocol, Sequence


# --------------------------------------------------------------------------
# Ledger row model
# --------------------------------------------------------------------------
#
# The ledger (台帳.json) is a JSON array of objects. The fields this package
# reads / writes are declared here; unknown fields are preserved verbatim on
# round-trip so we never lose data another subtask wrote.

LEDGER_FIELDS_READ = (
    "analysis_id",
    "dir",
    "verdict",
    "reasons",
    "K",
    "ci_std",
    "dmin",
    "add80",
    "kshort",
    "class",
    "next_trigger",
    "basis",
    "last_scanned",
)

# Fields this package is allowed to WRITE back on a row. Kept deliberately small:
# only the two the spec names (最終走査日 / 次の契機) plus an append-only scan note.
LEDGER_FIELDS_WRITE = ("last_scanned", "next_trigger", "scan_notes")


def today_iso(clock: Optional[Callable[[], _dt.date]] = None) -> str:
    """Return YYYY-MM-DD for 'today'. clock is injectable for deterministic tests."""
    d = (clock or _dt.date.today)()
    return d.isoformat()


def load_ledger(path: str | Path) -> list[dict[str, Any]]:
    """Read the ledger JSON array. Raises FileNotFoundError if missing."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("ledger root must be a JSON array")
    return data


def save_ledger(path: str | Path, rows: Sequence[dict[str, Any]]) -> None:
    """Write the ledger JSON array back, UTF-8, no BOM, trailing newline."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(list(rows), ensure_ascii=False, indent=2) + "\n"
    p.write_text(text, encoding="utf-8")


def find_rows(rows: Sequence[dict[str, Any]], classes: Iterable[str]) -> list[dict[str, Any]]:
    """Return rows whose 'class' is in the given set, preserving order."""
    want = set(classes)
    return [r for r in rows if r.get("class") in want]


def update_row(
    row: dict[str, Any],
    *,
    last_scanned: str,
    next_trigger: Optional[str] = None,
    scan_note: Optional[str] = None,
) -> dict[str, Any]:
    """Mutate a ledger row in place for an updated scan.

    Only the write-allowed fields are touched. scan_note is appended to an
    append-only 'scan_notes' list so no prior note is ever overwritten — a
    non-GO analysis keeps its full scan history, which is how we prove it was
    never silently dropped.
    """
    row["last_scanned"] = last_scanned
    if next_trigger is not None:
        row["next_trigger"] = next_trigger
    if scan_note is not None:
        notes = row.get("scan_notes")
        if not isinstance(notes, list):
            notes = []
        notes.append({"date": last_scanned, "note": scan_note})
        row["scan_notes"] = notes
    return row


# --------------------------------------------------------------------------
# Injected contracts (subtask 2 provides concrete implementations)
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class LotCount:
    """Result of counting lots currently in the DB for one claim."""

    analysis_id: str
    k_now: int          # lots available now
    k_required: int     # lots required to reach the registered target (p80)
    # k_required is read from the registered judge_result capacity block; the
    # driver never invents it.

    @property
    def reached(self) -> bool:
        return self.k_now >= self.k_required

    @property
    def shortfall(self) -> int:
        return max(0, self.k_required - self.k_now)


class LotCounter(Protocol):
    """Counts lots in the DB for a claim. Subtask 2 implements this against the
    real DB; tests pass a fake. Signature is fixed here so both agree."""

    def __call__(self, analysis_id: str, *, dir_rel: str) -> LotCount: ...


class DataFetcher(Protocol):
    """Acquire a fresh confirmation dataset for a claim and return the path to
    the engine-input file the judge will read. Returns None if no rows were
    obtained (which the driver records as an input problem, not a verdict)."""

    def __call__(self, analysis_id: str, *, dir_rel: str) -> Optional[str]: ...


@dataclasses.dataclass(frozen=True)
class JudgeOutcome:
    """Minimal view of a judge_result the driver needs. Mirrors the real
    judge_result.json fields; the driver reads, never writes, these."""

    verdict: Optional[str]            # GO / HOLD / NA / ERROR / EXCLUDED ...
    reasons: tuple[str, ...]
    order_ok: Optional[bool]          # from registered trusted clock; None = unknown
    lock_sha256: Optional[str]        # the REGISTERED lock; driver reuses as-is
    raw: dict[str, Any] = dataclasses.field(default_factory=dict)


class JudgeFn(Protocol):
    """Runs the EXISTING judge on an input file for a registered claim and
    returns its outcome. The driver supplies the registered lock so the judge
    uses the registered trusted clock — the driver never fabricates ordering.
    Thresholds / Delta_min / corrections are the judge's own; not passed here."""

    def __call__(
        self,
        analysis_id: str,
        *,
        input_path: str,
        registered_lock_sha256: Optional[str],
        dir_rel: str,
    ) -> JudgeOutcome: ...


# --------------------------------------------------------------------------
# Reading the registered claim (capacity + lock) from a judge_result.json
# --------------------------------------------------------------------------


def read_registered_result(path: str | Path) -> Optional[dict[str, Any]]:
    """Load a judge_result.json. Returns the dict, or None if absent/unreadable.

    The file sometimes stores the verdict object nested under 'verdict' and
    sometimes flat; callers use the helpers below rather than guessing.
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _verdict_obj(result: dict[str, Any]) -> dict[str, Any]:
    """Return the nested verdict object if present, else the result itself."""
    v = result.get("verdict")
    if isinstance(v, dict):
        return v
    return result


def registered_lock_sha256(result: dict[str, Any]) -> Optional[str]:
    """Extract the REGISTERED lock hash (trusted clock) from a judge_result.

    Never synthesised: if the registered result carries no lock, we return None
    and the order stays not-established.
    """
    vo = _verdict_obj(result)
    hashes = vo.get("hashes")
    if isinstance(hashes, dict):
        return hashes.get("lock_sha256")
    return None


def registered_k_required(result: dict[str, Any], *, level: str = "p80") -> Optional[int]:
    """Read the registered capacity target (k_required_p80 by default).

    This is the judge's own computed requirement; the driver reuses it and does
    not recompute or alter it.
    """
    vo = _verdict_obj(result)
    diag = vo.get("diagnostics")
    if not isinstance(diag, dict):
        return None
    cap = diag.get("capacity")
    if not isinstance(cap, dict):
        return None
    key = "k_required_p80" if level == "p80" else "k_required_p90"
    val = cap.get(key)
    if isinstance(val, (int, float)):
        return int(val)
    return None


def outcome_from_result(result: dict[str, Any]) -> JudgeOutcome:
    """Build a JudgeOutcome view from a loaded judge_result dict."""
    vo = _verdict_obj(result)
    verdict = vo.get("verdict")
    reasons = vo.get("reasons")
    if isinstance(reasons, str):
        reasons_t: tuple[str, ...] = tuple(
            x for x in reasons.replace(";", ",").split(",") if x
        )
    elif isinstance(reasons, list):
        reasons_t = tuple(str(x) for x in reasons)
    else:
        reasons_t = ()
    order_ok = vo.get("order_ok")
    return JudgeOutcome(
        verdict=verdict if isinstance(verdict, str) else None,
        reasons=reasons_t,
        order_ok=order_ok if isinstance(order_ok, bool) else None,
        lock_sha256=registered_lock_sha256(result),
        raw=result,
    )
