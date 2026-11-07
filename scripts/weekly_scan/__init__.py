# -*- coding: utf-8 -*-
"""Weekly-scan common foundation package.

This package holds the SHARED building blocks the weekly manufacturing-data scan is
built on. It deliberately contains no scanning policy of its own -- it only provides:

* ``ledger_io``  -- read/write the scan ledger (``\u53f0\u5e33.json``): one record per analysis,
  carrying its id, its not-adopted classification (A..E), the next trigger, and the
  date it was last scanned.
* ``judge_wrapper`` -- a thin, READ-ONLY reader over an existing judge result folder
  (``judge_result.json`` + ``manifest.json``). It never runs the judge, never
  regenerates a verdict, and never touches the order / trusted-registration clock.
  The registered claim and its recorded order are used exactly as they already are.
* ``db_lots`` -- open a named ODBC connection (reusing the server's ``tools.odbc_ops``
  convention, ``MCP_DB_<NAME>``, e.g. ADAMS) and count lots, stratified by product.

Nothing here changes the judge, its thresholds, Delta-min, the correction, or the
order clock. Those are read as already registered; they are never forged.
"""

from __future__ import annotations

__all__ = [
    "ledger_io",
    "judge_wrapper",
    "db_lots",
]

__version__ = "0.1.0"
