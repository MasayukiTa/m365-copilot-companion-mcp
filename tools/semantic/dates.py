# -*- coding: utf-8 -*-
"""Relative date phrases -> absolute half-open date ranges [start, end).

Every range is half-open: ``start`` is included and ``end`` is NOT. Write metric SQL as
``>= :start AND < :end``. The base date ("today") is always an argument so results are
reproducible and testable.

Conventions (stated here once, repeated in every result so nobody has to guess):

* "last N days / weeks / months / years" (also: 過去N日, 直近N日, past N days ...): a window of
  N units that ENDS TODAY AND INCLUDES TODAY. Today 2026-10-14, "past 1 month" is
  [2026-09-15, 2026-10-15). A 7-day window is [today-6, today+1).
* "last month" / "this month" / "last week" / "this week" / "last year" / "this year": whole
  calendar units (weeks start on Monday). "This month" is the WHOLE month, including days that
  have not happened yet.
* "yesterday" / "today": one day.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional, Union

from ._text import norm

CONVENTION = "start inclusive, end exclusive (use >= :start AND < :end)"

_KANJI_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
              "十": 10, "十一": 11, "十二": 12}


@dataclass
class Period:
    phrase: str                 # the text that was recognised (normalised)
    start: date
    end: date                   # exclusive
    label: str                  # canonical description, e.g. "last 1 month"
    rule: str                   # how it was computed, in words
    span: tuple                 # (start, end) offsets in the normalised text

    def as_dict(self) -> dict:
        return {"phrase": self.phrase, "label": self.label, "start": self.start.isoformat(),
                "end": self.end.isoformat(), "convention": CONVENTION, "rule": self.rule}


def to_date(value: Union[None, str, date, datetime]) -> date:
    """`today` argument -> date. None means the real current date."""
    if value is None or value == "":
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip()[:10])


def add_months(d: date, n: int) -> date:
    """Shift by whole months, clamping the day (Mar 31 - 1 month = Feb 28/29)."""
    m0 = d.year * 12 + (d.month - 1) + n
    y, m = divmod(m0, 12)
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def _first_of_month(d: date) -> date:
    return d.replace(day=1)


def _num(s: str) -> int:
    s = s.strip()
    if s.isdigit():
        return int(s)
    return _KANJI_NUM.get(s, 0)


_N = r"(\d+|十[一二]?|[一二三四五六七八九])"
_UNIT_JA = r"(日間|日|週間|週|[かヶカケヵ箇]月間?|年間|年)"
_UNIT_EN = r"(day|week|month|year)s?"

# Order matters: longer / more specific patterns first. Each entry: (regex, handler name).
_PATTERNS = [
    (re.compile(r"(?:過去|直近|最近|ここ|この)" + _N + _UNIT_JA), "ja_window"),
    (re.compile(r"(?:過去|直近|最近|ここ)半年"), "half_year"),
    (re.compile(r"(?:past|last|previous|recent)\s+(\d+)\s*" + _UNIT_EN), "en_window"),
    (re.compile(r"先々月"), "month_-2"),
    (re.compile(r"先月|前月|last month|previous month"), "month_-1"),
    (re.compile(r"今月|当月|本月|this month|current month"), "month_0"),
    (re.compile(r"先週|前週|last week|previous week"), "week_-1"),
    (re.compile(r"今週|this week|current week"), "week_0"),
    (re.compile(r"昨年|去年|前年|last year|previous year"), "year_-1"),
    (re.compile(r"今年|本年|当年|this year|current year"), "year_0"),
    (re.compile(r"昨日|きのう|yesterday"), "day_-1"),
    (re.compile(r"今日|本日|きょう|\btoday\b"), "day_0"),
]

_UNIT_KEY = {"日": "day", "週": "week", "月": "month", "年": "year",
             "day": "day", "week": "week", "month": "month", "year": "year"}


def _unit_key(u: str) -> str:
    u = u.strip()
    if u.startswith("日"):
        return "day"
    if u.startswith("週"):
        return "week"
    if u.endswith("月") or u.endswith("月間"):
        return "month"
    if u.startswith("年"):
        return "year"
    return _UNIT_KEY.get(u, "day")


def _window(today: date, n: int, unit: str) -> tuple:
    end = today + timedelta(days=1)
    if unit == "day":
        start = today - timedelta(days=n - 1)
    elif unit == "week":
        start = today - timedelta(days=7 * n - 1)
    elif unit == "month":
        start = add_months(today, -n) + timedelta(days=1)
    else:
        start = add_months(today, -12 * n) + timedelta(days=1)
    return start, end


def _build(handler: str, m: "re.Match", today: date) -> Optional[Period]:
    span = (m.start(), m.end())
    phrase = m.group(0)
    if handler in ("ja_window", "en_window"):
        n = _num(m.group(1))
        if n <= 0 or n > 3650:
            return None
        unit = _unit_key(m.group(2))
        start, end = _window(today, n, unit)
        return Period(phrase, start, end, "last %d %s%s" % (n, unit, "" if n == 1 else "s"),
                      "%d %s(s) ending today, today included" % (n, unit), span)
    if handler == "half_year":
        start, end = _window(today, 6, "month")
        return Period(phrase, start, end, "last 6 months",
                      "6 months ending today, today included", span)
    kind, _, off = handler.partition("_")
    off_i = int(off)
    if kind == "month":
        first = add_months(_first_of_month(today), off_i)
        return Period(phrase, first, add_months(first, 1),
                      {0: "this month", -1: "last month", -2: "two months ago"}[off_i],
                      "whole calendar month", span)
    if kind == "week":
        monday = today - timedelta(days=today.weekday()) + timedelta(days=7 * off_i)
        return Period(phrase, monday, monday + timedelta(days=7),
                      "this week" if off_i == 0 else "last week",
                      "whole week, Monday to Sunday", span)
    if kind == "year":
        y = today.year + off_i
        return Period(phrase, date(y, 1, 1), date(y + 1, 1, 1),
                      "this year" if off_i == 0 else "last year", "whole calendar year", span)
    if kind == "day":
        d = today + timedelta(days=off_i)
        return Period(phrase, d, d + timedelta(days=1),
                      "today" if off_i == 0 else "yesterday", "one day", span)
    return None


def find_periods(text: str, today: Union[None, str, date] = None) -> List[Period]:
    """All relative-date phrases in `text`, in order, non-overlapping. Offsets are in norm(text)."""
    base = to_date(today)
    t = norm(text)
    found: List[Period] = []
    taken: List[tuple] = []
    for rx, handler in _PATTERNS:
        for m in rx.finditer(t):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            p = _build(handler, m, base)
            if p is not None:
                found.append(p)
                taken.append(p.span)
    found.sort(key=lambda p: p.span)
    return found


def resolve_period(phrase: str, today: Union[None, str, date] = None) -> Optional[Period]:
    """The phrase as one period, or None when it is not (only) a relative-date phrase."""
    t = norm(phrase)
    ps = find_periods(t, today)
    if len(ps) == 1:
        s, e = ps[0].span
        rest = (t[:s] + t[e:]).strip(" 、,。.の")
        if not rest:
            return ps[0]
    return None


def mask_periods(text: str, periods: List[Period]) -> str:
    """norm(text) with every recognised phrase blanked out (same length, offsets unchanged)."""
    t = list(norm(text))
    for p in periods:
        for i in range(p.span[0], p.span[1]):
            t[i] = " "
    return "".join(t)
