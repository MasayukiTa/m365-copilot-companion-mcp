# -*- coding: utf-8 -*-
"""data_access_ledger -- the DATA-OPENING LEDGER for the validity harness.

WHY THIS EXISTS (owner 「やること」(1), 2026-11-03). A confirmation analysis is only
independent if the lots it reads were NOT already opened by an earlier analysis before the
claim was registered. The loss analysis (mcl_loss_ftype_confirm) returned order_ok=true / GO
yet its confirmation data fell inside a period an earlier analysis had already opened, so the
GO was an artefact of clock order, not of independence. To let the gate SEE that, every time an
analysis pulls lots out of the database we append one row here: which lots (the set), when they
were fetched, the exposure/response columns in play, and the analysis id. Nothing in this
module relaxes any judging threshold; it only RECORDS, so a stricter check downstream becomes
possible.

WHAT THIS MODULE IS, AND IS NOT.
  * It is the append-only RECORD of data openings and the reader/initialiser for it. One JSON
    object per line (JSONL), appended atomically, never rewritten in place.
  * It is NOT the independence check and NOT the gate. Computing the overlap fraction and
    flipping order_ok to false lives in relay/judge_gate.py (a separate task). This module only
    gives that check a truthful ledger to read.

WHERE THE LEDGER LIVES. Default under the operator's 生記録 tree:
  <USERPROFILE>/Documents/製造データ解析_生記録/開封台帳/data_access_ledger.jsonl
The absolute path is NOT hard-coded (a machine-specific path would leak a local account name
into a public repo); it is built from USERPROFILE/HOME at run time, or taken verbatim from the
DATA_ACCESS_LEDGER_PATH environment variable when set.

ROW SCHEMA (kept byte-compatible with the existing back-filled ledger so old and new rows join).
The fields a reader of this ledger relies on:
  analysis_id       str   -- id of the analysis that opened the data
  analysis_dir      str   -- directory of that analysis' judge request (may be "")
  judge_request     str   -- path to the analysis' judge_request.json (may be "")
  data_location     str   -- path the lots were read from
  data_ext          str   -- extension of that file (".csv"/".xlsx"/...)
  unit_column       str|None     -- the lot/unit column name
  exposure_column   str|None     -- the exposure column name
  response_column   str|None     -- the response column name
  fetched_at        str   -- ISO-8601 time the data was fetched (opening time)
  registered_at     str|None     -- ISO-8601 time the claim was registered, when known
  input_columns     list[str]    -- the columns present in the opened data
  n_rows            int   -- rows read
  n_lots            int   -- distinct lots
  lots              list[str]    -- the sorted distinct lot ids opened
  lots_sha256       str   -- sha256 over "\n".join(sorted distinct lots)
  status            str   -- "ok" when lots were read; else a reason (e.g. "no_unit_col")
  source            str   -- how the row was produced ("live" | "backfill")

Data minimisation: lot ids are production identifiers, not personal data; this module records no
measurement value, no employee id, and no raw row.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from typing import Iterable, Optional

#: Environment override for the ledger file. When set, used verbatim (so a test or an operator
#: can point the ledger somewhere else without editing code).
LEDGER_PATH_ENV = "DATA_ACCESS_LEDGER_PATH"

#: Subpath under the user's home where the ledger lives by default. Kept as path parts so the
#: os-native separator is used and no account name is committed.
_LEDGER_SUBPARTS = (
    "Documents",
    "製造データ解析_生記録",
    "開封台帳",
    "data_access_ledger.jsonl",
)

#: The row fields, in a fixed order, so every row written by this module has the same shape.
ROW_FIELDS = (
    "analysis_id",
    "analysis_dir",
    "judge_request",
    "data_location",
    "data_ext",
    "unit_column",
    "exposure_column",
    "response_column",
    "fetched_at",
    "registered_at",
    "input_columns",
    "n_rows",
    "n_lots",
    "lots",
    "lots_sha256",
    "status",
    "source",
)


def _home() -> str:
    return os.environ.get("USERPROFILE") or os.path.expanduser("~")


def ledger_path() -> str:
    """Absolute path to the ledger file (env override, else built from home).

    The directory is NOT created here; call init_ledger() (or record_access, which calls it) to
    create it. Reading uses this path directly.
    """
    env = os.environ.get(LEDGER_PATH_ENV)
    if env:
        return env
    return os.path.join(_home(), *_LEDGER_SUBPARTS)


def lots_sha256(lots: Iterable[str]) -> str:
    """sha256 over the distinct, sorted lot ids joined by newlines.

    Defined once so the writer and any reader agree on the fingerprint. The fingerprint is over
    the SAME value stored in the row's ``lots`` field (sorted + de-duplicated), so a reader can
    verify the field without re-deriving the order.
    """
    uniq = sorted({str(x).strip() for x in lots if str(x).strip()})
    return hashlib.sha256("\n".join(uniq).encode("utf-8")).hexdigest()


def init_ledger(path: Optional[str] = None) -> str:
    """Ensure the ledger's directory exists and the file is present (empty if new).

    Idempotent: creating a ledger that already exists leaves it untouched (it is NEVER
    truncated). Returns the ledger path. Safe to call before every append.
    """
    p = path or ledger_path()
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    if not os.path.exists(p):
        # Create empty; do not write a header -- JSONL has none.
        with open(p, "a", encoding="utf-8"):
            pass
    return p


def _normalise_lots(lots: Optional[Iterable[str]]) -> list:
    if not lots:
        return []
    return sorted({str(x).strip() for x in lots if str(x).strip()})


def build_row(
    analysis_id: str,
    *,
    lots: Optional[Iterable[str]] = None,
    data_location: str = "",
    data_ext: str = "",
    unit_column: Optional[str] = None,
    exposure_column: Optional[str] = None,
    response_column: Optional[str] = None,
    fetched_at: Optional[str] = None,
    registered_at: Optional[str] = None,
    input_columns: Optional[Iterable[str]] = None,
    analysis_dir: str = "",
    judge_request: str = "",
    n_rows: Optional[int] = None,
    status: str = "ok",
    source: str = "live",
) -> dict:
    """Assemble one ledger row (a dict) without writing it.

    Pure and side-effect free, so it can be unit-tested and so record_access and a back-fill can
    share one shape. ``lots`` is de-duplicated and sorted; ``n_lots`` and ``lots_sha256`` are
    derived from that same de-duplicated set. ``fetched_at`` defaults to now (local, ISO-8601
    with offset) because the FETCH TIME is the opening time the independence check orders
    against, and a row with no time cannot be joined.
    """
    uniq = _normalise_lots(lots)
    cols = list(input_columns) if input_columns else []
    if fetched_at is None:
        fetched_at = _now_iso()
    row = {
        "analysis_id": analysis_id,
        "analysis_dir": analysis_dir or "",
        "judge_request": judge_request or "",
        "data_location": data_location or "",
        "data_ext": data_ext or "",
        "unit_column": unit_column,
        "exposure_column": exposure_column,
        "response_column": response_column,
        "fetched_at": fetched_at,
        "registered_at": registered_at,
        "input_columns": cols[:40],
        "n_rows": int(n_rows) if n_rows is not None else len(uniq),
        "n_lots": len(uniq),
        "lots": uniq,
        "lots_sha256": lots_sha256(uniq),
        "status": status,
        "source": source,
    }
    return row


def _now_iso() -> str:
    # Local time with UTC offset, matching the existing ledger rows' fetched_at style.
    t = time.localtime()
    off = -(time.altzone if t.tm_isdst and time.daylight else time.timezone)
    sign = "+" if off >= 0 else "-"
    off = abs(off)
    hh = off // 3600
    mm = (off % 3600) // 60
    base = time.strftime("%Y-%m-%dT%H:%M:%S", t)
    return "%s%s%02d:%02d" % (base, sign, hh, mm)


def append_row(row: dict, path: Optional[str] = None) -> str:
    """Append one already-built row as a single JSON line. Returns the ledger path.

    The append is the atomic unit: one open in append mode, one write of exactly one line, one
    close. JSONL has no surrounding array to corrupt, so a crash mid-run leaves a truncated last
    line at worst, never a broken file. The directory/file are ensured first via init_ledger.
    """
    p = init_ledger(path)
    line = json.dumps(row, ensure_ascii=False)
    with open(p, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return p


def record_access(
    analysis_id: str,
    lots: Optional[Iterable[str]] = None,
    *,
    data_location: str = "",
    data_ext: str = "",
    unit_column: Optional[str] = None,
    exposure_column: Optional[str] = None,
    response_column: Optional[str] = None,
    fetched_at: Optional[str] = None,
    registered_at: Optional[str] = None,
    input_columns: Optional[Iterable[str]] = None,
    analysis_dir: str = "",
    judge_request: str = "",
    n_rows: Optional[int] = None,
    status: str = "ok",
    source: str = "live",
    path: Optional[str] = None,
) -> dict:
    """Record ONE data opening and return the row that was written.

    This is the API the harness calls every time an analysis fetches lots from the database.
    Call it AT FETCH TIME (when the data is opened), BEFORE the claim is registered -- the
    independence check orders a confirmation's lots against the fetch times already in the
    ledger, so an opening that is not recorded cannot be caught. ``registered_at`` may be left
    None here and the register-vs-fetch order recorded by the register step.
    """
    row = build_row(
        analysis_id,
        lots=lots,
        data_location=data_location,
        data_ext=data_ext,
        unit_column=unit_column,
        exposure_column=exposure_column,
        response_column=response_column,
        fetched_at=fetched_at,
        registered_at=registered_at,
        input_columns=input_columns,
        analysis_dir=analysis_dir,
        judge_request=judge_request,
        n_rows=n_rows,
        status=status,
        source=source,
    )
    append_row(row, path=path)
    return row


def read_ledger(path: Optional[str] = None) -> list:
    """Read every ledger row into a list of dicts (empty list if the file is absent).

    A malformed last line (e.g. a crash mid-append) is skipped rather than raised, so a reader
    can always make progress; every well-formed earlier line is still returned.
    """
    p = path or ledger_path()
    if not os.path.exists(p):
        return []
    rows = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                # Truncated/garbled line (crash mid-append): skip, keep the rest.
                continue
    return rows


def iter_openings(path: Optional[str] = None):
    """Yield (analysis_id, fetched_at, lot) for every lot in every ok row.

    A convenience the independence check can build on: it flattens the ledger to the grain the
    overlap fraction is computed at -- one lot opened at one time by one analysis. Rows whose
    status is not "ok" (no lots read) contribute nothing.
    """
    for row in read_ledger(path):
        if row.get("status") != "ok":
            continue
        aid = row.get("analysis_id")
        ft = row.get("fetched_at")
        for lot in row.get("lots") or ():
            yield aid, ft, lot


__all__ = [
    "LEDGER_PATH_ENV",
    "ROW_FIELDS",
    "ledger_path",
    "lots_sha256",
    "init_ledger",
    "build_row",
    "append_row",
    "record_access",
    "read_ledger",
    "iter_openings",
]
