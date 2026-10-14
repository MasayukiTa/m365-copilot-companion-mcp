# -*- coding: utf-8 -*-
"""Turn a metric definition into SQL with bind variables. Never runs the SQL.

* The metric SQL uses named parameters (``:start``, ``:end`` ...). They are rewritten to ``?``
  and the values are returned separately, in order, as ``bind``. A value is NEVER spliced into
  the SQL text.
* Relative dates ("past 1 month", "last month", "this month", "last 7 days", and the Japanese
  equivalents) are resolved to absolute dates against a base date that the caller can pass,
  and the resolved range is reported in the result.
* Only a single SELECT / WITH statement is accepted. Anything else is refused.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from . import dates
from .dates import find_periods, resolve_period, to_date          # noqa: F401  (re-exported)
from .store import Store, get_store

READ_ONLY_STARTS = ("select", "with")

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|exec|execute|merge|grant|revoke|"
    r"into|openrowset|openquery|opendatasource|bulk|waitfor|shutdown|backup|restore|"
    r"sp_executesql|sp_configure|sp_oacreate|call|declare|set|use)\b|\bxp_\w+")

_START_END_NAMES = (("start", "end"), ("start_date", "end_date"), ("from", "to"),
                    ("date_from", "date_to"), ("begin", "end"))


# ------------------------------------------------------------------ SQL tokenising

def _split(sql: str) -> List[Tuple[str, str]]:
    """Split SQL into (kind, text): code | string | ident | comment. Quotes are respected."""
    out: List[Tuple[str, str]] = []
    i, n = 0, len(sql)
    buf = []

    def flush():
        if buf:
            out.append(("code", "".join(buf)))
            buf.clear()

    while i < n:
        c = sql[i]
        two = sql[i:i + 2]
        if two == "--":
            j = sql.find("\n", i)
            j = n if j == -1 else j
            flush()
            out.append(("comment", sql[i:j]))
            i = j
        elif two == "/*":
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            flush()
            out.append(("comment", sql[i:j]))
            i = j
        elif c in ("'", '"', "["):
            close = {"'": "'", '"': '"', "[": "]"}[c]
            j = i + 1
            while j < n:
                if sql[j] == close:
                    if close != "]" and sql[j + 1:j + 2] == close:      # doubled quote
                        j += 2
                        continue
                    break
                j += 1
            j = min(j + 1, n)
            flush()
            out.append(("string" if c == "'" else "ident", sql[i:j]))
            i = j
        else:
            buf.append(c)
            i += 1
    flush()
    return out


def check_read_only(sql: str) -> Optional[str]:
    """None when `sql` is a single SELECT / WITH statement, else the reason it is refused."""
    if not isinstance(sql, str) or not sql.strip():
        return "the metric has no SQL"
    code = " ".join(t for k, t in _split(sql) if k == "code").lower()
    code = re.sub(r"\s+", " ", code).strip()
    body = code.rstrip("; ").strip()
    if ";" in body:
        return "more than one statement (a ';' inside the SQL)"
    first = re.match(r"[\s(]*([a-z_]+)", body)
    if not first or first.group(1) not in READ_ONLY_STARTS:
        return "only SELECT / WITH statements are accepted (starts with %r)" % (
            first.group(1) if first else body[:20])
    bad = _FORBIDDEN.search(body)
    if bad:
        return "contains %r, which is not allowed in a read-only metric" % bad.group(0)
    return None


def bind_named(sql: str) -> Tuple[str, List[str]]:
    """Rewrite ``:name`` to ``?`` (outside strings, identifiers and comments).

    Returns (sql_with_question_marks, names_in_order). A name used twice appears twice, once
    per ``?``. ``::`` casts are left alone.
    """
    names: List[str] = []
    rx = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")

    def sub(m):
        names.append(m.group(1))
        return "?"

    parts = []
    for kind, text in _split(sql):
        parts.append(rx.sub(sub, text) if kind == "code" else text)
    return "".join(parts), names


# ------------------------------------------------------------------ parameter values

def _coerce(p: Dict[str, Any], value: Any) -> Any:
    t = p.get("type", "string")
    name = p["name"]
    if value is None:
        raise ValueError("parameter %r is null" % name)
    if t == "date":
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        s = str(value).strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}([ T].*)?", s):
            raise ValueError("parameter %r must be a date like 2026-01-31, got %r" % (name, value))
        try:
            return date.fromisoformat(s[:10]).isoformat()
        except ValueError:
            raise ValueError("parameter %r must be a date like 2026-01-31, got %r" % (name, value))
    if t == "int":
        if isinstance(value, bool):
            raise ValueError("parameter %r must be an integer, got a boolean" % name)
        try:
            f = float(value)
            if f != int(f):
                raise ValueError
            return int(f)
        except (TypeError, ValueError):
            raise ValueError("parameter %r must be an integer, got %r" % (name, value))
    if t == "float":
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValueError("parameter %r must be a number, got %r" % (name, value))
    if t == "bool":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "y"):
            return True
        if s in ("0", "false", "no", "n"):
            return False
        raise ValueError("parameter %r must be true or false, got %r" % (name, value))
    return str(value)


def _period_targets(decl: List[Dict[str, Any]]) -> Optional[Tuple[str, str]]:
    names = [p["name"] for p in decl if p.get("type") == "date"]
    lower = {n.lower(): n for n in names}
    for a, b in _START_END_NAMES:
        if a in lower and b in lower:
            return lower[a], lower[b]
    if len(names) >= 2:
        return names[0], names[1]
    return None


def _fail(msg: str, **extra) -> Dict[str, Any]:
    out = {"ok": False, "error": msg}
    out.update(extra)
    return out


def metric_sql(name: str, params: Optional[Dict[str, Any]] = None, period: str = "",
               today=None, store: Optional[Store] = None) -> Dict[str, Any]:
    """Build the bind-variable SQL for the metric `name`.

    `params` are explicit values. `period` is a relative or absolute-free phrase such as
    "past 1 month" / "last month" / "this month" / "last 7 days" (or Japanese); it fills the
    metric's start/end date parameters unless those were given explicitly. `today` (ISO date)
    is the base date for relative phrases and defaults to the real date.
    """
    st = store if store is not None else get_store()
    err = st.error()
    if err:
        return _fail(err, configured=st.configured)
    metric = st.find_metric(name)
    if metric is None:
        known = [m["name"] for m in st.metrics][:30]
        return _fail("no metric named %r in the dictionary" % name, known_metrics=known)

    bad = check_read_only(metric.get("sql", ""))
    if bad:
        return _fail("metric %r refused: %s" % (metric["name"], bad), metric=metric["name"])

    try:
        base = to_date(today)
    except ValueError:
        return _fail("today must be an ISO date like 2026-10-14, got %r" % (today,))

    decl = {p["name"]: p for p in metric.get("params", [])}
    given = dict(params or {})
    unknown = [k for k in given if k not in decl]
    if unknown:
        return _fail("unknown parameter(s) %s; this metric declares: %s"
                     % (", ".join(map(repr, unknown)), ", ".join(decl) or "(none)"),
                     metric=metric["name"])

    resolved_dates: List[Dict[str, Any]] = []
    notes: List[str] = []
    values: Dict[str, Any] = {}

    # 1. period phrase fills the start/end pair (or one date for a one-day phrase)
    if period and str(period).strip():
        per = resolve_period(str(period), base)
        if per is None:
            return _fail("could not read %r as a relative date. Examples: 'past 1 month', "
                         "'last month', 'this month', 'last 7 days', '先月', '過去1か月', "
                         "'直近7日'. Or pass explicit start/end dates in params." % period,
                         metric=metric["name"])
        target = _period_targets(list(decl.values()))
        one_day = (per.end - per.start).days == 1
        date_names = [n for n, p in decl.items() if p.get("type") == "date"]
        if target:
            s_name, e_name = target
            entry = {"phrase": per.phrase, "label": per.label, "base_date": base.isoformat(),
                     "rule": per.rule, "convention": dates.CONVENTION, "filled": {}}
            if s_name not in given:
                values[s_name] = per.start.isoformat()
                entry["filled"][s_name] = values[s_name]
            if e_name not in given:
                values[e_name] = per.end.isoformat()
                entry["filled"][e_name] = values[e_name]
            resolved_dates.append(entry)
        elif len(date_names) == 1 and one_day:
            n0 = date_names[0]
            if n0 not in given:
                values[n0] = per.start.isoformat()
            resolved_dates.append({"phrase": per.phrase, "label": per.label,
                                   "base_date": base.isoformat(), "rule": per.rule,
                                   "filled": {n0: values.get(n0)}})
        else:
            return _fail("metric %r has no start/end date parameters to fill from %r"
                         % (metric["name"], period), metric=metric["name"])

    # 2. explicit values (win over the period), coerced to the declared type
    for k, v in given.items():
        try:
            values[k] = _coerce(decl[k], v)
        except ValueError as e:
            return _fail(str(e), metric=metric["name"])

    # 3. defaults for what is still missing
    for k, p in decl.items():
        if k not in values and p.get("default") is not None:
            try:
                values[k] = _coerce(p, p["default"])
            except ValueError as e:
                return _fail("default of " + str(e), metric=metric["name"])
            notes.append("parameter %r not given; used the default %r" % (k, values[k]))

    sql_q, order = bind_named(metric["sql"])
    undeclared = sorted({n for n in order if n not in decl})
    if undeclared:
        return _fail("the SQL uses parameter(s) %s that the metric does not declare in 'params'"
                     % ", ".join(":" + n for n in undeclared), metric=metric["name"])
    missing = [n for n in dict.fromkeys(order) if n not in values]
    if missing:
        return _fail("missing parameter(s): %s" % ", ".join(missing), metric=metric["name"],
                     missing=missing,
                     declared=[{"name": p["name"], "type": p["type"], "default": p.get("default")}
                               for p in metric.get("params", [])])
    unused = [n for n in values if n not in order]
    if unused:
        notes.append("parameter(s) not used by the SQL: %s" % ", ".join(unused))

    out: Dict[str, Any] = {
        "ok": True, "metric": metric["name"], "description": metric.get("description", ""),
        "sql": sql_q, "bind": [values[n] for n in order], "bind_names": order,
        "params": {n: values[n] for n in dict.fromkeys(order)},
        "resolved_dates": resolved_dates, "status": metric.get("status", "draft"),
        "caveats": metric.get("caveats", []), "dimensions": metric.get("dimensions", []),
        "tables": metric.get("tables", []), "executed": False,
        "how_to_run": "Pass 'sql' and 'bind' to a driver that binds '?' parameters. The "
                      "values are not part of the SQL text.",
        "notes": notes,
    }
    if out["status"] != "certified":
        out["notes"].append("metric is a draft (not certified); verify the result with its owner")
    return out
