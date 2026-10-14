# -*- coding: utf-8 -*-
"""Time zone and timestamp helpers. JST is the default; tzdata is not required for it."""
from __future__ import annotations

import datetime as _dt
import re

DEFAULT_TZ = "JST"


def parse_tz(name):
    """A tzinfo from 'JST', 'UTC', '+09:00', '-0500' or an IANA name (needs tzdata)."""
    text = str(name or DEFAULT_TZ).strip()
    up = text.upper()
    if up in ("JST", "ASIA/TOKYO"):
        return _dt.timezone(_dt.timedelta(hours=9), "JST")
    if up in ("UTC", "Z", "GMT"):
        return _dt.timezone.utc
    m = re.fullmatch(r"([+-])(\d{1,2}):?(\d{2})?", text)
    if m:
        delta = _dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        return _dt.timezone(delta if m.group(1) == "+" else -delta)
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo(text)
    except Exception:
        raise ValueError("unknown time zone: %r (use JST, UTC, +09:00 or an IANA name)" % text)


def parse_time(value, tz):
    """Epoch seconds from 'YYYY-MM-DD[ T]HH:MM[:SS]', a date, or a raw epoch number."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if re.fullmatch(r"\d{9,11}(\.\d+)?", text):
        return float(text)
    text = text.replace("T", " ").replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            naive = _dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=tz).timestamp()
    raise ValueError("cannot read time %r (use 'YYYY-MM-DD HH:MM[:SS]')" % text)


def fmt_time(ts, tz) -> str:
    if ts is None:
        return ""
    return _dt.datetime.fromtimestamp(ts, tz).strftime("%H:%M:%S")


def fmt_dt(ts, tz) -> str:
    if ts is None:
        return ""
    return _dt.datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M:%S")


def fmt_duration(seconds, lang="ja") -> str:
    """1030 -> '17分10秒' (ja) / '17m10s' (en). Seconds are rounded to the nearest whole."""
    if seconds is None:
        return ""
    s = int(round(float(seconds)))
    if s < 60 and abs(float(seconds)) < 10 and float(seconds) != 0:
        return ("%.2f秒" if lang == "ja" else "%.2fs") % float(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if lang == "ja":
        if h:
            return "%d時間%d分%d秒" % (h, m, sec)
        if m:
            return "%d分%d秒" % (m, sec)
        return "%d秒" % sec
    if h:
        return "%dh%02dm%02ds" % (h, m, sec)
    if m:
        return "%dm%02ds" % (m, sec)
    return "%ds" % sec
