"""periodic_scan - generic periodic scan of registered analyses.

This package runs on a schedule over a backlog ledger and, for each kind of
“not-yet-adopted” analysis, decides whether anything can now be confirmed and
re-runs a frozen validity judge where appropriate.

The package is GENERIC: it hardcodes no database name, table, column, query,
analysis id or business term. Everything site-specific is read at call time from
a local, gitignored config (``.config/scan_profile.json``; see ``docs/CONFIG.md``
for a documented fake example) or injected by the caller. Unit tests use only
synthetic fixtures.

Modules:
  - config        : load the local scan profile (connection/table/column names)
  - ledger_io     : read/write the backlog ledger JSON (state only)
  - judge_wrapper : call the existing validity judge (criteria/thresholds reused
                    verbatim; the registration-order trust clock is never forged)
  - db_lots       : count NEW lots, stratified by a product column
  - runner        : wire ledger + lot counting + judge into one scan pass
  - bc_confirm    : processing (a) B/C confirmation
  - d_reacquire   : processing (b) D re-acquisition

The judge, its criteria, effect-size floor and any standardisation/correction are
NEVER changed here, and the registration-order trust clock is NEVER forged: a
re-confirmation reuses the already-registered timestamp verbatim.
"""

from __future__ import annotations

# Classification codes (a small, fixed inventory).
CLASS_A = "A"  # effect shown absent (interval inside the floor, enough lots)
CLASS_B = "B"  # post-hoc, registration order not established (held on ordering)
CLASS_C = "C"  # too few lots (records how many more are needed)
CLASS_D = "D"  # acquisition/entry defect suspected (needs re-acquisition)
CLASS_E = "E"  # not judgeable (structural problem)

VALID_CLASSES = frozenset({CLASS_A, CLASS_B, CLASS_C, CLASS_D, CLASS_E})

# Classes re-scanned on each pass. A and E are out of scope (A = settled absent,
# E = structurally unjudgeable).
RESCAN_CLASSES = frozenset({CLASS_B, CLASS_C, CLASS_D})

__all__ = [
    "CLASS_A",
    "CLASS_B",
    "CLASS_C",
    "CLASS_D",
    "CLASS_E",
    "VALID_CLASSES",
    "RESCAN_CLASSES",
]
