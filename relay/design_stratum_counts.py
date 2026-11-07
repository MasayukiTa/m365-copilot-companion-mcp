# -*- coding: utf-8 -*-
"""Build exposure-group x stratum LOT COUNTS from a CSV, WITHOUT reading the response column.

WHY THIS EXISTS.  The design-confounding screen in ``tools/confound_check.py`` judges a
comparison's DESIGN -- whether the two exposure groups also differ in product / line /
period -- from a column-oriented ``rows`` dict it is handed.  Something has to BUILD that
input from the actual analysis CSV first, and it must do so WITHOUT EVER OPENING THE
RESPONSE (outcome) column: a design verdict that depends on the result is not a design
verdict.  This module is that adapter.

WHAT THIS DOES.  Given a registration ``claim`` (its ``hierarchy.unit_column`` =
lot/unit column and ``exposure.column``) and a CSV path, it:
  1. reads ONLY the header to discover columns;
  2. picks the stratifier columns (product name / grade / class / line / period / equipment)
     by the same token list the screen uses, excluding exposure / response / unit columns;
  3. reads ONLY the exposure, unit and stratifier columns from the CSV -- the response
     column is named in the claim but is never added to the read set, so its cells are
     never parsed;
  4. returns, per exposure group, per stratifier, a {stratum_level: lot_count} dict, where
     a 'lot' is a distinct value of the unit column (so repeated rows of one lot count once).

WHEN STRATIFIER COLUMNS ARE ABSENT.  If no stratifier column is present in the CSV header,
the design cannot be checked from the file alone.  ``build_stratum_counts`` then calls the
DB-join path (``join_product_from_db``) to try to attach product/grade columns by the unit
key.  The default DB path is a STUB that returns no mapping (so the caller still learns the
stratifier is missing); a real ODBC resolver can be injected via ``db_resolver``.

RESPONSE COLUMN IS NEVER OPENED.  The response column name is accepted (so the adapter knows
what to EXCLUDE) but is never placed in the set of columns read from the CSV.  This is the
core invariant and is asserted by the tests.

HERMETIC.  Standard library only; the only I/O is reading the given CSV text file; no network
unless a real ``db_resolver`` is injected (the default resolver touches nothing).
"""
from __future__ import annotations

import csv
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

# The stratifier token list is kept identical in spirit to tools/confound_check.STRATIFIER_TOKENS
# so the adapter and the screen agree on what a 'design axis' is.  Duplicated here as data (not
# imported) to keep this module usable without the tools package on the path; a test pins the
# two lists together so they cannot silently diverge.
STRATIFIER_TOKENS: Tuple[str, ...] = (
    "品名", "品種", "製品区分", "製品", "塗工布",
    "時期", "期間", "日付", "日時", "年月", "date", "time", "period",
    "ライン", "line",
    "設備", "機号", "機械", "equip", "machine",
)

#: Minimum lots per group within a shared stratum for that stratum to 'count' as usable.
#: This mirrors the goal's k_min and is NOT changed here; exported so callers read one value.
K_MIN_LOTS: int = 8


def is_stratifier_name(name: str) -> bool:
    """True when a column name looks like a stratification candidate (casefolded substring)."""
    if not isinstance(name, str):
        return False
    low = name.casefold()
    return any(tok.casefold() in low for tok in STRATIFIER_TOKENS if tok)


def find_stratifier_columns(columns: Sequence[str],
                            exclude: Optional[Sequence[str]] = None) -> List[str]:
    """Column names that look like stratifiers, in input order, minus ``exclude``."""
    ex: Set[str] = set(exclude or ())
    out: List[str] = []
    for c in columns or []:
        if c in ex:
            continue
        if is_stratifier_name(c) and c not in out:
            out.append(c)
    return out


def read_csv_header(csv_path: str, encoding: str = "utf-8-sig") -> List[str]:
    """Return the CSV header row (column names) WITHOUT reading any data rows."""
    with open(csv_path, "r", encoding=encoding, newline="") as fh:
        reader = csv.reader(fh)
        for row in reader:
            return [c.strip() for c in row]
    return []


def read_selected_columns(csv_path: str,
                          columns: Sequence[str],
                          forbidden: Sequence[str] = (),
                          encoding: str = "utf-8-sig") -> Dict[str, List[str]]:
    """Read ONLY ``columns`` from the CSV into a column-oriented dict.

    ``forbidden`` names columns that must never be read (the response column).  If any name in
    ``columns`` is also in ``forbidden``, this raises -- the adapter refuses to open the
    response column by construction, rather than trusting the caller to have filtered it.
    Columns in ``columns`` that are absent from the header are skipped silently.
    """
    want = list(dict.fromkeys(columns))
    forb = set(forbidden or ())
    overlap = [c for c in want if c in forb]
    if overlap:
        raise ValueError(
            "refusing to read response/forbidden column(s): " + ", ".join(map(str, overlap)))
    out: Dict[str, List[str]] = {c: [] for c in want}
    with open(csv_path, "r", encoding=encoding, newline="") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        present = [c for c in want if c in header]
        out = {c: [] for c in present}
        for rec in reader:
            for c in present:
                out[c].append((rec.get(c) or "").strip())
    return out


def lot_counts_by_group(exposure_vals: Sequence[Any],
                        unit_vals: Sequence[Any],
                        stratifier_vals: Sequence[Any]) -> Dict[str, Dict[str, int]]:
    """Per exposure group, per stratum level, the number of DISTINCT lots (unit values).

    A lot is counted once per (group, stratum) regardless of how many rows it has.  Blank
    exposure or unit values are ignored.  Returns {exposure_level: {stratum_level: lot_count}}.
    """
    if not (len(exposure_vals) == len(unit_vals) == len(stratifier_vals)):
        raise ValueError("exposure, unit and stratifier columns must be equal length")
    # group -> stratum -> set(lot)
    seen: Dict[str, Dict[str, Set[str]]] = {}
    for e, u, s in zip(exposure_vals, unit_vals, stratifier_vals):
        es, us = str(e), str(u)
        if es == "" or us == "":
            continue
        ss = str(s)
        seen.setdefault(es, {}).setdefault(ss, set()).add(us)
    return {g: {st: len(lots) for st, lots in strata.items()} for g, strata in seen.items()}


def join_product_from_db(unit_values: Sequence[Any],
                         db_resolver: Optional[Callable[[Sequence[Any]], Mapping[Any, Mapping[str, Any]]]] = None,
                         ) -> Dict[Any, Dict[str, Any]]:
    """DB-join fallback: map each lot/unit value to stratifier attributes (product/grade/line).

    This is the seam for pulling product name etc. from the manufacturing DB over ODBC when the
    analysis CSV does not carry them.  The real resolver is injected as ``db_resolver`` and is
    expected to run a parameterised query keyed by the unit values and return
    ``{unit_value: {column_name: value, ...}}``.

    The DEFAULT (``db_resolver is None``) is a STUB that returns an empty mapping -- it touches
    no network and no driver.  An empty mapping is a truthful 'could not attach a stratifier',
    which the caller surfaces as STRATIFIER_MISSING rather than inventing levels.
    """
    if db_resolver is None:
        return {}
    resolved = db_resolver(list(unit_values)) or {}
    return {k: dict(v) for k, v in resolved.items()}


def build_stratum_counts(claim: Mapping[str, Any],
                         csv_path: str,
                         db_resolver: Optional[Callable[[Sequence[Any]], Mapping[Any, Mapping[str, Any]]]] = None,
                         encoding: str = "utf-8-sig",
                         ) -> Dict[str, Any]:
    """Build exposure-group x stratum lot counts from the CSV, never opening the response column.

    claim: registration claim; uses ``exposure.column``, ``hierarchy.unit_column`` and
           (only to EXCLUDE it) ``response.column``.
    csv_path: path to the analysis CSV.
    db_resolver: optional ODBC resolver for the DB-join fallback (see ``join_product_from_db``).

    Returns a dict:
      exposure_column, unit_column, response_column (echoed, for audit)
      response_column_read -- ALWAYS False; proof the response was never in the read set
      stratifier_columns   -- stratifier columns found in the CSV header
      source               -- 'csv' | 'db_join' | 'none'
      counts               -- {stratifier_column: {exposure_level: {stratum: lot_count}}}
      db_join_called        -- whether the DB fallback was invoked
    """
    exposure = (claim or {}).get("exposure", {}) or {}
    exposure_col = exposure.get("column")
    unit_col = ((claim or {}).get("hierarchy", {}) or {}).get("unit_column")
    response_col = ((claim or {}).get("response", {}) or {}).get("column")

    header = read_csv_header(csv_path, encoding=encoding)
    exclude = [c for c in (exposure_col, response_col, unit_col) if c]
    strat_cols = find_stratifier_columns(header, exclude=exclude)

    result: Dict[str, Any] = {
        "exposure_column": exposure_col,
        "unit_column": unit_col,
        "response_column": response_col,
        "response_column_read": False,
        "stratifier_columns": list(strat_cols),
        "source": "none",
        "counts": {},
        "db_join_called": False,
        "k_min_lots": K_MIN_LOTS,
    }

    # The response column is the one column we must never read; pass it as forbidden so the
    # reader will raise if it ever slips into the want-list.
    forbidden = [c for c in (response_col,) if c]

    if strat_cols:
        want = [c for c in (exposure_col, unit_col, *strat_cols) if c]
        data = read_selected_columns(csv_path, want, forbidden=forbidden, encoding=encoding)
        exp_vals = data.get(exposure_col, []) if exposure_col else []
        unit_vals = data.get(unit_col, []) if unit_col else []
        counts: Dict[str, Any] = {}
        for sc in strat_cols:
            strat_vals = data.get(sc, [])
            if len(strat_vals) == len(exp_vals) == len(unit_vals):
                counts[sc] = lot_counts_by_group(exp_vals, unit_vals, strat_vals)
        result["counts"] = counts
        result["source"] = "csv"
        return result

    # No stratifier column in the CSV: try the DB-join fallback keyed by the unit column.
    result["db_join_called"] = True
    if exposure_col and unit_col:
        data = read_selected_columns(
            csv_path, [exposure_col, unit_col], forbidden=forbidden, encoding=encoding)
        exp_vals = data.get(exposure_col, [])
        unit_vals = data.get(unit_col, [])
        joined = join_product_from_db(unit_vals, db_resolver=db_resolver)
        if joined:
            # Build a synthetic stratifier column from the joined attribute(s).
            attr_names: List[str] = sorted({k for v in joined.values() for k in v.keys()})
            counts2: Dict[str, Any] = {}
            for attr in attr_names:
                strat_vals = [str(joined.get(u, {}).get(attr, "")) for u in unit_vals]
                counts2[attr] = lot_counts_by_group(exp_vals, unit_vals, strat_vals)
            result["counts"] = counts2
            result["stratifier_columns"] = attr_names
            result["source"] = "db_join"
            return result

    # Nothing in the CSV and nothing from the DB: the stratifier is genuinely missing.
    result["source"] = "none"
    return result


def shared_stratum_summary(group_counts: Mapping[str, Mapping[str, int]],
                           k_min: int = K_MIN_LOTS) -> Dict[str, Any]:
    """Summarise, for ONE stratifier's {group: {stratum: lots}}, the shared design capacity.

    Returns: the exposure groups seen, the strata shared by all groups, and how many of those
    shared strata have at least ``k_min`` lots in EVERY group (the strata where the exposure
    effect is actually separable).  This reads only lot counts -- never a response.
    """
    groups = sorted(group_counts.keys())
    if not groups:
        return {"groups": [], "shared_strata": 0, "shared_strata_meeting_k_min": 0, "k_min": k_min}
    strata_sets = [set(group_counts[g].keys()) for g in groups]
    shared = set.intersection(*strata_sets) if strata_sets else set()
    meeting = 0
    for st in shared:
        if all(int(group_counts[g].get(st, 0)) >= k_min for g in groups):
            meeting += 1
    return {
        "groups": groups,
        "shared_strata": len(shared),
        "shared_strata_meeting_k_min": meeting,
        "k_min": k_min,
    }
