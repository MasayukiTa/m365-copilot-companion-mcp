# -*- coding: utf-8 -*-
"""Small text helpers shared by the semantic layer: normalisation, script runs, bigrams.

No morphological analyser is used on purpose (it would be one more dependency to install on
a fresh machine). Japanese is handled by splitting text where the script changes -- kanji
runs and katakana runs carry the content words, hiragana runs are mostly particles and
inflections -- plus character bigrams for partial matching. That is crude, and every caller
treats a weak match as a *suggestion*, never as a definition.
"""
from __future__ import annotations

import re
import unicodedata
from typing import List, NamedTuple


def norm(text) -> str:
    """NFKC (folds full-width / half-width forms), case-folded, whitespace collapsed."""
    if text is None:
        return ""
    s = unicodedata.normalize("NFKC", str(text)).casefold()
    return re.sub(r"\s+", " ", s).strip()


def _kind(ch: str) -> str:
    o = ord(ch)
    if 0x3040 <= o <= 0x309F:
        return "hira"
    if 0x30A0 <= o <= 0x30FF or o == 0x31F0 or 0x31F0 <= o <= 0x31FF:
        return "kata"           # includes the prolonged sound mark U+30FC
    if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or o == 0x3005:
        return "han"
    if ch.isascii() and (ch.isalpha() or ch == "_"):
        return "latin"
    if ch.isdigit():
        return "digit"
    if ch.isalpha():
        return "latin"          # accented letters etc.: treat like latin words
    return "other"


class Run(NamedTuple):
    kind: str
    text: str
    start: int
    end: int


def script_runs(text: str) -> List[Run]:
    """Split already-normalised text into maximal runs of one script."""
    runs: List[Run] = []
    i = 0
    n = len(text)
    while i < n:
        k = _kind(text[i])
        j = i + 1
        while j < n and _kind(text[j]) == k:
            j += 1
        runs.append(Run(k, text[i:j], i, j))
        i = j
    return runs


def content_runs(text: str) -> List[Run]:
    """Runs that can carry a content word: kanji, katakana, latin (len >= 2 or any cjk)."""
    out = []
    for r in script_runs(text):
        if r.kind in ("han", "kata") and len(r.text) >= 1:
            out.append(r)
        elif r.kind == "latin" and len(r.text) >= 2:
            out.append(r)
    return out


def stem(word: str) -> str:
    """Very light English stemmer: enough for plurals and -ing / -ed on short words."""
    w = word
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and re.search(r"(ss|x|z|ch|sh)es$", w):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    if len(w) > 5 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 4 and w.endswith("ed"):
        return w[:-2]
    return w


def latin_words(text: str) -> List[Run]:
    """Latin words (letters, digits, underscore) with their spans, stemmed in `.text`."""
    out = []
    for m in re.finditer(r"[a-z0-9_]+", text):
        w = m.group(0)
        out.append(Run("latin", stem(w), m.start(), m.end()))
    return out


def is_pure_latin(s: str) -> bool:
    return bool(s) and all(c.isascii() and (c.isalnum() or c in " _-.") for c in s)


def bigrams(s: str) -> set:
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def dice(a: str, b: str) -> float:
    A, B = bigrams(a), bigrams(b)
    if not A or not B:
        return 0.0
    return 2.0 * len(A & B) / (len(A) + len(B))


def as_str_list(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if isinstance(v, (str, int, float)) and str(v).strip()]
    return []
