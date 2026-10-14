"""Result normalisation and comparison (execution accuracy).

A result is ``Result(columns, rows)``. Two results are compared after normalising every cell:

* NULL: ``None``, NaN and the empty string are all NULL (a CSV round trip cannot tell them apart).
* numbers: int / float / Decimal / numeric strings become floats and compare within a tolerance.
* dates: ISO-like strings and date/datetime objects become ``YYYY-MM-DD`` (midnight) or
  ``YYYY-MM-DD HH:MM:SS``.
* other strings: stripped, otherwise compared exactly (case sensitive).

Columns are aligned by name first, then by content, then by position, so a renamed or reordered
column does not by itself make an answer wrong.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

DEFAULT_TOL = 1e-6
_GREEDY_LIMIT = 2000

_NUM_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_DATE_RE = re.compile(
    r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})"
    r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?)?(?:Z)?$"
)


@dataclass
class Result:
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Any) -> "Result":
        if isinstance(d, Result):
            return d
        if not isinstance(d, dict):
            raise ValueError("result must be an object with 'columns' and 'rows'")
        cols = [str(c) for c in d.get("columns") or []]
        rows = []
        for r in d.get("rows") or []:
            if isinstance(r, dict):
                rows.append([r.get(c) for c in cols])
            else:
                rows.append(list(r))
        if not cols and rows and isinstance(d.get("rows")[0], dict):
            cols = list(d["rows"][0].keys())
            rows = [[r.get(c) for c in cols] for r in d["rows"]]
        return cls(cols, rows)

    def to_dict(self) -> dict:
        return {"columns": list(self.columns), "rows": [list(r) for r in self.rows]}


@dataclass
class CompareConfig:
    order: str = "ignore"  # "ignore" | "strict"
    columns: str = "exact"  # "exact" | "subset" (answer may carry extra columns)
    rows: str = "exact"  # "exact" | "subset" (answer may carry extra rows)
    tol: float = DEFAULT_TOL

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "CompareConfig":
        d = d or {}
        cfg = cls(
            order=str(d.get("order", "ignore")),
            columns=str(d.get("columns", "exact")),
            rows=str(d.get("rows", "exact")),
            tol=float(d.get("tol", DEFAULT_TOL)),
        )
        if cfg.order not in ("ignore", "strict"):
            raise ValueError("compare.order must be 'ignore' or 'strict'")
        if cfg.columns not in ("exact", "subset"):
            raise ValueError("compare.columns must be 'exact' or 'subset'")
        if cfg.rows not in ("exact", "subset"):
            raise ValueError("compare.rows must be 'exact' or 'subset'")
        if cfg.tol < 0:
            raise ValueError("compare.tol must be >= 0")
        return cfg


# ---------------------------------------------------------------------------- cells

def norm_cell(v: Any) -> Any:
    """Return None (NULL), ("n", float), ("d", str) or ("s", str)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return ("n", 1.0 if v else 0.0)
    if isinstance(v, (int, float, Decimal)):
        f = float(v)
        return None if math.isnan(f) else ("n", f + 0.0)
    if isinstance(v, _dt.datetime):
        return ("d", _fmt_dt(v.year, v.month, v.day, v.hour, v.minute, v.second))
    if isinstance(v, _dt.date):
        return ("d", "%04d-%02d-%02d" % (v.year, v.month, v.day))
    if isinstance(v, (bytes, bytearray)):
        v = bytes(v).decode("utf-8", "replace")
    s = str(v).strip()
    if s == "":
        return None
    if _NUM_RE.match(s):
        return ("n", float(s) + 0.0)
    m = _DATE_RE.match(s)
    if m:
        y, mo, d, hh, mi, ss = (int(x) if x else 0 for x in m.groups())
        try:
            _dt.datetime(y, mo, d, hh, mi, ss)
            return ("d", _fmt_dt(y, mo, d, hh, mi, ss))
        except ValueError:
            pass
    if s.lower() in ("nan",):
        return None
    return ("s", s)


def _fmt_dt(y, mo, d, hh, mi, ss) -> str:
    if hh == 0 and mi == 0 and ss == 0:
        return "%04d-%02d-%02d" % (y, mo, d)
    return "%04d-%02d-%02d %02d:%02d:%02d" % (y, mo, d, hh, mi, ss)


def _decimals(tol: float) -> int:
    if tol <= 0:
        return 12
    return max(0, min(12, int(math.ceil(-math.log10(tol)))))


def cell_key(c: Any, decimals: int) -> tuple:
    """Hashable, totally ordered key for a normalised cell (numbers bucketed by rounding)."""
    if c is None:
        return (0, 0)
    kind, val = c
    if kind == "n":
        return (1, round(val, decimals) + 0.0)
    if kind == "d":
        return (2, val)
    return (3, val)


def cells_equal(a: Any, b: Any, tol: float) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if a[0] != b[0]:
        return False
    if a[0] == "n":
        return math.isclose(a[1], b[1], rel_tol=tol, abs_tol=tol)
    return a[1] == b[1]


def _norm_rows(res: Result) -> list[list[Any]]:
    width = len(res.columns)
    out = []
    for r in res.rows:
        r = list(r)
        if width and len(r) < width:
            r += [None] * (width - len(r))
        out.append([norm_cell(c) for c in r])
    return out


# ---------------------------------------------------------------------- alignment

def _col_signature(rows: list[list[Any]], j: int, decimals: int) -> tuple:
    return tuple(sorted(cell_key(r[j], decimals) for r in rows if j < len(r)))


def align_columns(exp: Result, act: Result, tol: float, subset: bool) -> Optional[list[int]]:
    """For every expected column, the index of the matching answer column (or None if impossible)."""
    ne, na = len(exp.columns), len(act.columns)
    if ne == 0:
        return []
    if not subset and ne != na:
        return None
    if na < ne:
        return None
    dec = _decimals(tol)
    erows, arows = _norm_rows(exp), _norm_rows(act)
    mapping: list[Optional[int]] = [None] * ne
    used: set[int] = set()
    by_name: dict[str, list[int]] = {}
    for j, name in enumerate(act.columns):
        by_name.setdefault(str(name).strip().casefold(), []).append(j)
    # 1. same name AND same content
    for i, name in enumerate(exp.columns):
        sig = _col_signature(erows, i, dec)
        for j in by_name.get(str(name).strip().casefold(), []):
            if j not in used and _col_signature(arows, j, dec) == sig:
                mapping[i] = j
                used.add(j)
                break
    # 2. same content under another name
    for i in range(ne):
        if mapping[i] is not None:
            continue
        sig = _col_signature(erows, i, dec)
        for j in range(na):
            if j not in used and _col_signature(arows, j, dec) == sig:
                mapping[i] = j
                used.add(j)
                break
    # 3. same name, content differs (tolerance edge cases, wrong values): keep so the row
    #    comparison reports the real difference instead of "column missing"
    for i, name in enumerate(exp.columns):
        if mapping[i] is not None:
            continue
        for j in by_name.get(str(name).strip().casefold(), []):
            if j not in used:
                mapping[i] = j
                used.add(j)
                break
    # 4. positional for what is left (exact mode only)
    if any(m is None for m in mapping):
        if subset:
            return None
        free = [j for j in range(na) if j not in used]
        for i in range(ne):
            if mapping[i] is None:
                mapping[i] = free.pop(0)
    return [int(m) for m in mapping]  # type: ignore[arg-type]


# ------------------------------------------------------------------------ compare

def _rows_multiset_match(erows, arows, tol, subset) -> bool:
    dec = _decimals(tol)
    ek = Counter(tuple(cell_key(c, dec) for c in r) for r in erows)
    ak = Counter(tuple(cell_key(c, dec) for c in r) for r in arows)
    if subset:
        if all(ak[k] >= n for k, n in ek.items()):
            return True
    elif ek == ak:
        return True
    # tolerance edge case: bucket boundaries -> greedy pairwise match
    if tol <= 0 or len(erows) > _GREEDY_LIMIT or len(arows) > _GREEDY_LIMIT:
        return False
    if not subset and len(erows) != len(arows):
        return False
    pool = list(arows)
    for er in erows:
        hit = None
        for k, ar in enumerate(pool):
            if all(cells_equal(x, y, tol) for x, y in zip(er, ar)):
                hit = k
                break
        if hit is None:
            return False
        pool.pop(hit)
    return True


def _rows_ordered_match(erows, arows, tol, subset) -> bool:
    if not subset:
        return len(erows) == len(arows) and all(
            all(cells_equal(x, y, tol) for x, y in zip(er, ar)) for er, ar in zip(erows, arows)
        )
    k = 0
    for er in erows:
        while k < len(arows) and not all(cells_equal(x, y, tol) for x, y in zip(er, arows[k])):
            k += 1
        if k == len(arows):
            return False
        k += 1
    return True


def compare_results(expected: Result, actual: Result, cfg: Optional[CompareConfig] = None) -> tuple[bool, str]:
    """Return (match, reason). ``reason`` is empty on a match."""
    cfg = cfg or CompareConfig()
    sub_cols = cfg.columns == "subset"
    mapping = align_columns(expected, actual, cfg.tol, sub_cols)
    if mapping is None:
        if not sub_cols and len(expected.columns) != len(actual.columns):
            return False, "column count differs (%d expected, %d returned)" % (
                len(expected.columns), len(actual.columns))
        return False, "expected columns not found in the answer"
    erows = _norm_rows(expected)
    arows_full = _norm_rows(actual)
    arows = [[r[j] if j < len(r) else None for j in mapping] for r in arows_full]
    sub_rows = cfg.rows == "subset"
    if not sub_rows and len(erows) != len(arows):
        return False, "row count differs (%d expected, %d returned)" % (len(erows), len(arows))
    if sub_rows and len(erows) > len(arows):
        return False, "answer has fewer rows (%d) than the %d required" % (len(arows), len(erows))
    if cfg.order == "strict":
        ok = _rows_ordered_match(erows, arows, cfg.tol, sub_rows)
    else:
        ok = _rows_multiset_match(erows, arows, cfg.tol, sub_rows)
    return (True, "") if ok else (False, "cell values differ")


def canonical_signature(res: Result, tol: float = DEFAULT_TOL) -> str:
    """Order- and name-insensitive fingerprint of a result, for repeat-agreement checks."""
    dec = _decimals(tol)
    rows = _norm_rows(res)
    ncol = len(res.columns)
    order = sorted(range(ncol), key=lambda j: repr(_col_signature(rows, j, dec)))
    keyed = sorted(tuple(cell_key(r[j], dec) for j in order) for r in rows)
    return hashlib.sha1(repr((ncol, keyed)).encode("utf-8")).hexdigest()[:16]
