# -*- coding: utf-8 -*-
"""design_stratum_counts -- the DESIGN-side counting that the judge claim preflight runs
before any analysis, WITHOUT ever reading the response column.

WHY THIS MODULE EXISTS.
  relay.judge_claim_preflight runs a design preflight that must answer one question: can the
  two exposure groups even be compared WITHIN a shared stratum (the product / line / period
  that an owner's confound objection says must be held fixed)? Answering it requires counting
  how many distinct lots each exposure group contributes in each stratum. It does NOT require
  -- and must never touch -- the response column: the design verdict has to be provably
  independent of the outcome, so this module reads only the stratifier column(s), the exposure
  column and the unit (lot) column of the claim's data file.

  This is the counting half of the seam whose estimation half is relay.stratified_estimate.
  That module reads y to estimate once a design is judged to hold; THIS module reads no y at
  all and only decides whether the design can hold. Keeping the two apart is what lets the
  design decision be proven outcome-blind.

WHAT THE PREFLIGHT NEEDS FROM HERE (the exact API judge_claim_preflight imports):
  build_stratum_counts(claim, data_location) -> dict with keys
      stratifier_columns     list[str]  -- the stratifier columns declared by the claim
      counts                 {column: {group: {stratum: lot_count}}} -- distinct lots per cell
      response_column_read   bool       -- always False; a defensive witness that the response
                                            column was never opened (the preflight refuses to
                                            turn a True into a verdict)
  shared_stratum_summary(counts, k_min) -> dict with keys
      k_min                              -- the threshold used (echoed back)
      shared_strata                      -- strata present in BOTH groups
      shared_strata_meeting_k_min        -- of those, how many have >= k_min lots in EACH group
  K_MIN_LOTS                             -- the default minimum lots per group within a stratum

Standard library only.
"""
from __future__ import annotations

import csv
import os

#: Minimum distinct lots PER GROUP within a stratum for that stratum to count as a usable
#: within-stratum contrast. Mirrors the engine's k_min (8) and relay.stratified_estimate's
#: DEFAULT_K_MIN by owner instruction; it is NOT the engine's threshold and does not change it.
K_MIN_LOTS = 8

__all__ = [
    "K_MIN_LOTS",
    "build_stratum_counts",
    "shared_stratum_summary",
]


def _stratifier_columns(claim):
    """Return the stratifier column names declared by the claim, in a stable order.

    The claim may declare its stratifiers in any of a few shapes, so each is accepted:
      claim['stratifiers']                 -> list[str] or list[{'column': str}]
      claim['stratifier']                  -> str, {'column': str} or {'columns': [str, ...]}
      claim['design']['stratifiers']       -> list[str] or list[{'column': str}]
    Unknown / missing declarations yield an empty list (the preflight then reports that no
    stratifier is present). Duplicates are removed while preserving first-seen order.
    """
    def _as_names(value):
        names = []
        if value is None:
            return names
        if isinstance(value, str):
            if value:
                names.append(value)
            return names
        if isinstance(value, dict):
            if isinstance(value.get("columns"), (list, tuple)):
                for item in value["columns"]:
                    names.extend(_as_names(item))
            elif value.get("column"):
                names.append(value["column"])
            return names
        if isinstance(value, (list, tuple)):
            for item in value:
                names.extend(_as_names(item))
            return names
        return names

    candidates = []
    if isinstance(claim, dict):
        candidates.append(claim.get("stratifiers"))
        candidates.append(claim.get("stratifier"))
        design = claim.get("design")
        if isinstance(design, dict):
            candidates.append(design.get("stratifiers"))
            candidates.append(design.get("stratifier"))

    ordered = []
    seen = set()
    for cand in candidates:
        for name in _as_names(cand):
            if name not in seen:
                seen.add(name)
                ordered.append(name)
    return ordered


def _column(claim, section, key):
    """Return claim[section][key] when both exist and the value is a non-empty string."""
    if isinstance(claim, dict):
        obj = claim.get(section)
        if isinstance(obj, dict):
            val = obj.get(key)
            if isinstance(val, str) and val:
                return val
    return None


def _exposure_column(claim):
    return _column(claim, "exposure", "column")


def _unit_column(claim):
    return _column(claim, "hierarchy", "unit_column")


def build_stratum_counts(claim, data_location):
    """Count distinct lots per (stratifier column, exposure group, stratum) for ``claim``.

    Reads ONLY the stratifier column(s), the exposure column and the unit (lot) column of the
    CSV named by ``data_location``. The response column is never opened: csv.DictReader keeps
    every field as text and this function only ever indexes the three design columns, so a
    response column that would raise if parsed is still never parsed.

    Returns a dict:
      stratifier_columns    list[str]
      counts                {column: {group: {stratum: lot_count}}}, lot_count = number of
                            DISTINCT unit values seen in that (group, stratum) cell
      response_column_read  False (constant witness; see module docstring)

    When the stratifier, exposure or unit columns cannot be resolved, or the file cannot be
    read, the counts for the affected columns are simply empty -- the caller treats an empty
    design as a design that cannot support a within-stratum contrast, which is the correct and
    conservative reading. This function never raises for a missing column or unreadable file;
    it returns what it could count.
    """
    strat_cols = _stratifier_columns(claim)
    exposure_col = _exposure_column(claim)
    unit_col = _unit_column(claim)

    # {column: {group: {stratum: set(lot_ids)}}}
    lot_sets = {col: {} for col in strat_cols}

    if strat_cols and exposure_col and unit_col:
        wanted = set(strat_cols) | {exposure_col, unit_col}
        for row in _rows(data_location, wanted):
            group = row.get(exposure_col)
            lot = row.get(unit_col)
            if group is None or lot is None:
                continue
            for col in strat_cols:
                stratum = row.get(col)
                if stratum is None:
                    continue
                by_group = lot_sets[col].setdefault(group, {})
                by_group.setdefault(stratum, set()).add(lot)

    counts = {}
    for col, by_group in lot_sets.items():
        counts[col] = {
            group: {stratum: len(lots) for stratum, lots in by_stratum.items()}
            for group, by_stratum in by_group.items()
        }

    return {
        "stratifier_columns": list(strat_cols),
        "counts": counts,
        "response_column_read": False,
    }


def _rows(data_location, wanted_columns):
    """Yield dicts restricted to ``wanted_columns`` for each data row of the CSV.

    Only the wanted columns are pulled out of each row; every other field (including the
    response column) is left untouched as the raw text csv already produced -- it is never
    converted, compared or otherwise interpreted. A file that cannot be read yields nothing.
    """
    ext = os.path.splitext(data_location)[1].lower()
    if ext not in (".csv", ".txt"):
        return
    for encoding in ("utf-8-sig", "cp932"):
        try:
            with open(data_location, encoding=encoding, newline="") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames is None:
                    return
                present = [c for c in wanted_columns if c in reader.fieldnames]
                for row in reader:
                    yield {c: row.get(c) for c in present}
            return
        except (OSError, UnicodeError, csv.Error):
            continue


def shared_stratum_summary(counts, k_min=K_MIN_LOTS):
    """Summarise, for ONE stratifier column's counts, how many strata both groups share.

    ``counts`` is the per-column mapping {group: {stratum: lot_count}} (i.e. the value stored
    at build_stratum_counts()['counts'][column]).

    Returns:
      k_min                        the threshold used (echoed back)
      shared_strata                strata that appear in EVERY group present
      shared_strata_meeting_k_min  of those shared strata, how many have >= k_min lots in
                                   EACH group -- i.e. strata that can actually carry a
                                   within-stratum contrast

    With fewer than two groups there can be no shared stratum, so both counts are 0.
    """
    groups = list(counts.keys()) if isinstance(counts, dict) else []
    if len(groups) < 2:
        return {"k_min": k_min, "shared_strata": 0, "shared_strata_meeting_k_min": 0}

    strata_by_group = [set(counts[g].keys()) for g in groups]
    shared = set.intersection(*strata_by_group) if strata_by_group else set()

    meeting = 0
    for stratum in shared:
        if all(counts[g].get(stratum, 0) >= k_min for g in groups):
            meeting += 1

    return {
        "k_min": k_min,
        "shared_strata": len(shared),
        "shared_strata_meeting_k_min": meeting,
    }
