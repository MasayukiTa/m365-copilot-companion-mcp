"""Candidate column pairs, found without comparing every column with every other.

Columns are bucketed by (type family, collapsed value shape). Only columns that share a
bucket are compared, and within a bucket only pairs that involve a key-like column or share
a meaningful name token are considered.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

from .profile import ColumnProfile, collapse_shape

#: Tokens that say "this is an identifier" but not which one; they weigh little in name match.
GENERIC_TOKENS = frozenset({"id", "no", "num", "number", "nr", "code", "cd", "key", "idx", "番号", "コード"})

DEFAULT_ALIASES: tuple = (
    ("no", "num", "number", "nr", "番号"),
    ("cd", "code", "コード"),
    ("id", "identifier"),
    ("date", "dt", "日付"),
)

_COMPAT = {("int", "num"), ("num", "int")}


def families_compatible(a: str, b: str) -> bool:
    return a == b or (a, b) in _COMPAT


# ------------------------------------------------------------------------- names
_SPLIT = re.compile(
    r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+|[぀-ヿ㐀-鿿ｦ-ﾟ]+"
)


def name_tokens(name: str) -> list[str]:
    """Split an identifier into lower-case tokens (case, separators, digits, script changes).
    Full-width forms are normalised first (NFKC), so ``ＮＯ`` and ``NO`` are the same token."""
    text = unicodedata.normalize("NFKC", name)
    # NFKC turns half-width katakana into full-width; the split regex handles both.
    return [t.lower() for t in _SPLIT.findall(text)]


def _singular(tok: str) -> str:
    return tok[:-1] if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss") else tok


def build_alias_groups(aliases: Optional[Mapping[str, Iterable[str]]]) -> list[frozenset]:
    groups = [frozenset(unicodedata.normalize("NFKC", t).lower() for t in g) for g in DEFAULT_ALIASES]
    for key, values in (aliases or {}).items():
        groups.append(frozenset(unicodedata.normalize("NFKC", t).lower() for t in [key, *values]))
    return groups


def _canon(tok: str, groups: list[frozenset]) -> str:
    tok = _singular(tok)
    for g in groups:
        if tok in g:
            return min(g)
    return tok


def _weight(tok: str) -> float:
    return 0.3 if tok in GENERIC_TOKENS else 1.0


def _token_set(column: str, table: str, groups) -> set[str]:
    toks = {_canon(t, groups) for t in name_tokens(column)}
    if toks and all(t in {_canon(g, groups) for g in GENERIC_TOKENS} for t in toks):
        # A bare ``id`` / ``no`` column is named by its table (customers.id ~ customer_id).
        toks |= {_canon(t, groups) for t in name_tokens(table)}
    return toks


def name_similarity(col_a: str, table_a: str, col_b: str, table_b: str,
                    groups: list[frozenset]) -> float:
    """Weighted Dice similarity of name tokens; a Japanese token that contains another (length
    two or more) counts as a partial match."""
    ta, tb = _token_set(col_a, table_a, groups), _token_set(col_b, table_b, groups)
    if not ta or not tb:
        return 0.0
    gen = {_canon(g, groups) for g in GENERIC_TOKENS}
    w = lambda t: 0.3 if t in gen else 1.0  # noqa: E731

    def best(t: str, other: set[str]) -> float:
        if t in other:
            return 1.0
        if len(t) >= 2 and any(len(o) >= 2 and (t in o or o in t) and not t.isascii() for o in other):
            return 0.7
        return 0.0

    num = sum(w(t) * best(t, tb) for t in ta) + sum(w(t) * best(t, ta) for t in tb)
    den = sum(w(t) for t in ta) + sum(w(t) for t in tb)
    score = min(1.0, num / den) if den else 0.0
    specific = any(t not in gen and best(t, tb) > 0 for t in ta)
    # Sharing only "id"/"no"/"code" says nothing about which entity the column names.
    return score if specific else score * 0.1


# ------------------------------------------------------------------------- shapes / keys
def _shape_hist(p: ColumnProfile) -> dict[str, float]:
    hist: dict[str, float] = defaultdict(float)
    for shape, frac in p.patterns:
        hist[collapse_shape(shape)] += frac
    return hist


def shape_similarity(a: dict[str, float], b: dict[str, float]) -> float:
    """Histogram intersection of collapsed shapes (1.0 = identical mix, 0.0 = disjoint)."""
    return sum(min(f, b.get(s, 0.0)) for s, f in a.items())


def is_key_like(p: ColumnProfile) -> bool:
    return p.distinct_ratio >= 0.9 and p.null_rate <= 0.05 and p.distinct_est >= 2


def _range_overlap(a: ColumnProfile, b: ColumnProfile) -> float:
    try:
        if a.family in ("int", "num") and b.family in ("int", "num"):
            lo, hi = max(a.min_value, b.min_value), min(a.max_value, b.max_value)
            span = max(a.max_value, b.max_value) - min(a.min_value, b.min_value)
            if span <= 0:
                return 1.0
            return max(0.0, (hi - lo) / span)
    except TypeError:
        return 0.5
    return 1.0


def _len_compat(a: ColumnProfile, b: ColumnProfile) -> float:
    if a.family != "text" or b.family != "text" or not a.max_len or not b.max_len:
        return 1.0
    return min(a.max_len, b.max_len) / max(a.max_len, b.max_len)


@dataclass
class Candidate:
    a_table: str
    a_column: str
    b_table: str
    b_column: str
    score: float
    name_score: float = 0.0
    reasons: list = field(default_factory=list)


def generate_candidates(profiles: list[ColumnProfile], aliases: Optional[Mapping[str, Iterable[str]]] = None,
                        min_score: float = 0.3, max_candidates: Optional[int] = None) -> list[Candidate]:
    groups = build_alias_groups(aliases)
    usable = [p for p in profiles if p.family != "binary" and p.patterns]
    hists = {id(p): _shape_hist(p) for p in usable}
    keylike = {id(p): is_key_like(p) for p in usable}

    buckets: dict[tuple, list[ColumnProfile]] = defaultdict(list)
    for p in usable:
        fam = "n" if p.family in ("int", "num") else p.family
        for shape, frac in sorted(hists[id(p)].items(), key=lambda kv: (-kv[1], kv[0]))[:3]:
            if frac >= 0.2:
                buckets[(fam, shape)].append(p)

    seen: set[tuple] = set()
    out: list[Candidate] = []
    for members in buckets.values():
        by_token: dict[str, list[ColumnProfile]] = defaultdict(list)
        for m in members:
            for t in _token_set(m.column, m.table, groups) - {_canon(g, groups) for g in GENERIC_TOKENS}:
                by_token[t].append(m)
        pair_iter = []
        for p in members:
            if keylike[id(p)]:
                pair_iter.extend((p, q) for q in members)
        for group in by_token.values():
            pair_iter.extend((p, q) for p in group for q in group)
        for p, q in pair_iter:
            if p is q or p.table == q.table or not families_compatible(p.family, q.family):
                continue
            key = tuple(sorted([(p.table, p.column), (q.table, q.column)]))
            if key in seen:
                continue
            seen.add(key)
            # a = the referencing (less unique) side, b = the key-like side.
            a, b = (p, q) if (keylike[id(q)], -p.distinct_ratio, p.table, p.column) >= \
                             (keylike[id(p)], -q.distinct_ratio, q.table, q.column) else (q, p)
            name = name_similarity(a.column, a.table, b.column, b.table, groups)
            if not (keylike[id(a)] or keylike[id(b)] or name >= 0.5):
                continue
            shape = shape_similarity(hists[id(a)], hists[id(b)])
            if shape < 0.5:
                continue
            tl = 0.5 * _len_compat(a, b) + 0.5 * _range_overlap(a, b)
            kl = 1.0 if keylike[id(b)] else 0.0
            score = 0.45 * name + 0.25 * shape + 0.10 * tl + 0.20 * kl
            if score < min_score:
                continue
            reasons = [f"name={name:.2f}", f"shape={shape:.2f}", f"type/range={tl:.2f}",
                       "key-like parent" if kl else "non-unique both sides"]
            out.append(Candidate(a.table, a.column, b.table, b.column, round(score, 4), name, reasons))
    out.sort(key=lambda c: (-c.score, c.a_table, c.a_column, c.b_table, c.b_column))
    return out[:max_candidates] if max_candidates else out
