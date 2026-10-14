"""Column profiling from bounded samples (never a full scan of a large table)."""

from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .source import Source, type_family

SHAPE_CAP = 24  # characters of a value considered when computing its shape


@dataclass
class ProfileConfig:
    sample_rows: int = 20_000          # max rows read per table (all columns share one sample)
    big_table_rows: int = 1_000_000    # above this, never anything but the sample
    top_patterns: int = 8
    top_values: int = 10
    seed: int = 0


@dataclass
class ColumnProfile:
    table: str
    column: str
    declared_type: str
    family: str                       # int / num / text / date / binary (inferred when untyped)
    rows_est: int                     # approximate rows in the table
    sample_n: int                     # rows actually read
    complete: bool                    # True when the whole table was read (exact figures)
    null_rate: float
    distinct_est: float
    distinct_exact: bool
    min_value: Any = None
    max_value: Any = None
    min_len: int = 0
    max_len: int = 0
    patterns: list = field(default_factory=list)      # [(shape, fraction)]
    top_values: list = field(default_factory=list)    # [(value, count)]
    sample_hash: str = ""

    @property
    def nonnull_rows(self) -> float:
        return self.rows_est * (1.0 - self.null_rate)

    @property
    def distinct_ratio(self) -> float:
        nn = self.nonnull_rows
        return min(1.0, self.distinct_est / nn) if nn > 0 else 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ColumnProfile":
        d = dict(d)
        d["patterns"] = [tuple(p) for p in d.get("patterns", [])]
        d["top_values"] = [tuple(p) for p in d.get("top_values", [])]
        return cls(**d)


# ------------------------------------------------------------------------- value shapes
def shape_of(value: Any) -> str:
    """Shape signature: letters -> A, digits -> N, full-width/CJK -> W, symbols kept as is."""
    s = str(value)
    out = []
    for ch in s[:SHAPE_CAP]:
        if ch.isascii():
            out.append("A" if ch.isalpha() else "N" if ch.isdigit() else ("_" if ch.isspace() else ch))
        elif unicodedata.east_asian_width(ch) in ("W", "F", "H"):
            out.append("W")
        elif ch.isalpha():
            out.append("A")
        elif ch.isdigit():
            out.append("N")
        else:
            out.append(ch)
    if len(s) > SHAPE_CAP:
        out.append("~")
    return "".join(out)


def collapse_shape(shape: str) -> str:
    """Run-length collapse (``NNNN-NN`` -> ``N-N``): compares shapes across different widths."""
    return re.sub(r"(.)\1+", r"\1", shape)


# ------------------------------------------------------------------------- estimation
def estimate_distinct(counts: Counter, sample_n: int, population_nonnull: float) -> float:
    """Chao1-style distinct estimate from a sample, clamped to ``[observed, population]``."""
    d = len(counts)
    f1 = sum(1 for c in counts.values() if c == 1)
    f2 = sum(1 for c in counts.values() if c == 2)
    est = d + (f1 * f1) / (2.0 * f2) if f2 else d + f1 * (f1 - 1) / 2.0
    return float(max(d, min(est, population_nonnull)))


def _infer_family(declared: str, values: list) -> str:
    fam = type_family(declared)
    if fam != "any":
        return fam
    if not values:
        return "text"
    kinds = {type(v) for v in values[:200]}
    if kinds <= {int}:
        return "int"
    if kinds <= {int, float}:
        return "num"
    if kinds <= {bytes}:
        return "binary"
    return "text"


def _scalar(v: Any) -> Any:
    return v if isinstance(v, (int, float, str)) or v is None else str(v)


def _min_max(values: list) -> tuple[Any, Any]:
    try:
        return _scalar(min(values)), _scalar(max(values))
    except TypeError:
        strs = [str(v) for v in values]
        return min(strs), max(strs)


def column_hash(declared: str, values: list) -> str:
    h = hashlib.sha1()
    h.update(declared.encode("utf-8", "replace"))
    h.update(b"\0")
    for s in sorted({repr(v) for v in values}):
        h.update(s.encode("utf-8", "replace"))
        h.update(b"\0")
    h.update(f"|nulls={sum(1 for v in values if v is None)}".encode())
    return h.hexdigest()


def profile_column(table: str, column: str, declared: str, values: list, rows_est: int,
                   complete: bool, cfg: ProfileConfig) -> ColumnProfile:
    n = len(values)
    nonnull = [v for v in values if v is not None]
    null_rate = (n - len(nonnull)) / n if n else 0.0
    family = _infer_family(declared, nonnull)
    counts: Counter = Counter()
    if family != "binary":
        counts = Counter(nonnull)
    pop_nonnull = max(float(len(nonnull)), rows_est * (1.0 - null_rate))
    if complete:
        distinct, exact = float(len(counts)), True
    else:
        distinct, exact = estimate_distinct(counts, n, pop_nonnull), False
    prof = ColumnProfile(
        table=table, column=column, declared_type=declared, family=family,
        rows_est=int(rows_est), sample_n=n, complete=complete, null_rate=round(null_rate, 6),
        distinct_est=distinct, distinct_exact=exact, sample_hash=column_hash(declared, values),
    )
    if nonnull and family != "binary":
        prof.min_value, prof.max_value = _min_max(list(counts))
        lens = [len(str(v)) for v in counts]
        prof.min_len, prof.max_len = min(lens), max(lens)
        shapes = Counter(shape_of(v) for v in nonnull)
        total = sum(shapes.values())
        prof.patterns = [(s, round(c / total, 4)) for s, c in shapes.most_common(cfg.top_patterns)]
        prof.top_values = [(str(v), c) for v, c in counts.most_common(cfg.top_values)]
    return prof


@dataclass
class ProfileStats:
    tables_done: int = 0
    tables_skipped: int = 0           # run out of time budget
    columns_profiled: int = 0
    columns_reused: int = 0           # unchanged since the stored run
    skipped_reasons: dict = field(default_factory=dict)


def profile_source(source: Source, cfg: ProfileConfig, store=None,
                   deadline: Optional[float] = None) -> tuple[list[ColumnProfile], ProfileStats]:
    """Profile every table with one bounded sample per table.

    With a ``store``, a column whose (table, column, type, sample-hash) is unchanged keeps its
    stored profile; only changed or new columns are recomputed.
    """
    stats = ProfileStats()
    out: list[ColumnProfile] = []
    for table in source.list_tables():
        if deadline is not None and time.monotonic() > deadline:
            stats.tables_skipped += 1
            stats.skipped_reasons[table] = "run time budget exhausted"
            continue
        cols = source.columns(table)
        if not cols:
            continue
        rows_est = source.approx_rows(table) or 0
        rows, complete = source.sample(table, [c.name for c in cols], cfg.sample_rows, cfg.seed)
        if rows_est < len(rows):
            rows_est = len(rows)
        for idx, col in enumerate(cols):
            values = [r[idx] for r in rows]
            stored = store.get_profile(table, col.name) if store is not None else None
            if stored is not None and stored.declared_type == col.type \
                    and stored.sample_hash == column_hash(col.type, values):
                stored.rows_est = int(rows_est)
                out.append(stored)
                stats.columns_reused += 1
                continue
            prof = profile_column(table, col.name, col.type, values, rows_est, complete, cfg)
            out.append(prof)
            stats.columns_profiled += 1
            if store is not None:
                store.put_profile(prof)
        stats.tables_done += 1
    return out, stats
