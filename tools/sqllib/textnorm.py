"""Question normalisation for the verified-SQL library.

Goal: two phrasings of the same business question should land on (nearly) the same
character stream, so that a similarity score over it is meaningful.

What it does, in order:
  1. Unicode NFKC (full-width <-> half-width, compatibility forms) and lower-casing.
  2. Relative periods ("past 1 month", "last month", "yesterday", ...) become one shared
     placeholder; concrete dates become another; remaining numbers a third. Placeholders are
     single private-use characters so they behave as one unit in n-grams.
  3. Politeness / request phrases and the commonest one-character particles are dropped.
  4. Everything that is not a letter, digit or placeholder is dropped.
"""
from __future__ import annotations

import re
import unicodedata

REL = ""   # relative period: past 1 month, last month, yesterday ...
DATE = ""  # concrete date
NUM = ""   # any other number
PLACEHOLDERS = (REL, DATE, NUM)

_MONTH_UNIT = r"(?:か|ヶ|ケ|カ|ヵ|箇)?月"

_REL_PATTERNS = [
    # "past 1 month", "recent 30 days", "this 3 weeks"
    r"(?:過去|直近|最近|ここ|この|前の|最新の?)\s*\d*\s*(?:" + _MONTH_UNIT +
    r"|週間|週|日間|日|年間|年|四半期|時間)(?:間|以内|前)?",
    # "3 months ago / within 3 months" (number + unit + marker)
    r"\d+\s*(?:" + _MONTH_UNIT + r"|週間|日間|年間)(?:間|以内|前)",
    r"\d+\s*(?:" + _MONTH_UNIT + r")間?",
    # last month / this week / next year ...
    r"(?:先|今|来|前|当|昨|翌|本|去)(?:月度|月|週|年度|年|四半期|期)",
    r"(?:昨日|今日|本日|明日|一昨日|きのう|きょう|おととい|前日|翌日|当日)",
    # English
    r"(?:last|past|previous|prev|this|next|recent|current)\s+(?:\d+\s+)?"
    r"(?:day|week|month|year|quarter|hour)s?",
    r"\b(?:yesterday|today|tomorrow)\b",
    r"\d+\s+(?:day|week|month|year|quarter|hour)s?\s+ago",
]
_REL_RE = re.compile("|".join("(?:%s)" % p for p in _REL_PATTERNS))

_DATE_PATTERNS = [
    r"\d{4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}",
    r"\d{4}年\s*\d{1,2}月(?:\s*\d{1,2}日)?",
    r"\d{4}年度?",
    r"\d{1,2}月\s*\d{1,2}日",
    r"\d{1,2}月(?:度)?",
    r"\d{1,2}日",
    r"\d{1,2}/\d{1,2}",
    r"\b\d{8}\b",
]
_DATE_RE = re.compile("|".join("(?:%s)" % p for p in _DATE_PATTERNS))
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")

# Request / politeness phrases that carry no part of the question's meaning.
_FILLER_RE = re.compile(
    r"(?:教えてください|教えて下さい|教えて|ください|下さい|見せてください|見せて|"
    r"出してください|出して|知りたいです|知りたい|お願いします|お願い|"
    r"表示してください|表示して|してください|したいです|したい|"
    r"please|show me|tell me|give me|can you|could you)"
)
_PARTICLES = set("のをはがにでともへや")


def normalize(text: str) -> str:
    """Normalised, whitespace/punctuation-free character stream for matching."""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text).lower()
    s = _REL_RE.sub(REL, s)
    s = _DATE_RE.sub(DATE, s)
    s = _NUM_RE.sub(NUM, s)
    s = _FILLER_RE.sub(" ", s)
    out = []
    for ch in s:
        if ch in PLACEHOLDERS:
            out.append(ch)
        elif ch in _PARTICLES:
            continue
        elif unicodedata.category(ch)[0] in ("L", "N"):
            out.append(ch)
    return "".join(out)


_WORD_RE = re.compile(
    "[-]|[a-z0-9_]+|[一-鿿々]+|[゠-ヿー]+"
)


def words(norm: str) -> list[str]:
    """Content words of a normalised stream: ASCII words, kanji runs, katakana runs and
    placeholders. Hiragana-only runs are mostly grammar and are left to the n-grams."""
    return _WORD_RE.findall(norm)


def ngrams(norm: str, n: int = 2) -> set[str]:
    if not norm:
        return set()
    if len(norm) < n:
        return {norm}
    return {norm[i:i + n] for i in range(len(norm) - n + 1)}
