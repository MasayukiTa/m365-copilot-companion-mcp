"""Static inspection of a stored SQL example.

Pure string work, no database. It decides three things:
  * errors   -- the SQL must not be stored at all (not read-only, hidden, multi-statement)
  * warnings -- stored, but the entry is flagged (hard-coded literals, undeclared parameters)
  * facts    -- parameters used (``:name``) and tables referenced

The scanner understands single-quoted strings (with '' escapes), double-quoted / bracketed /
backtick identifiers, so a ';' or '--' inside a literal is not mistaken for syntax.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

ALLOWED_FIRST = ("select", "with")

#: Words that make a statement something other than a read. INTO catches SELECT ... INTO.
FORBIDDEN_WORDS = frozenset({
    "insert", "update", "delete", "drop", "alter", "create", "truncate", "merge",
    "exec", "execute", "grant", "revoke", "attach", "detach", "pragma", "vacuum",
    "reindex", "into", "replace_into", "call", "load", "copy", "set", "declare",
    "commit", "rollback", "begin", "savepoint", "backup", "restore", "use",
})

_PARAM_RE = re.compile(r"(?<![:\w]):([A-Za-z_][A-Za-z0-9_]*)")
_DATE_LITERAL_RE = re.compile(
    r"^\s*(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?|\d{8}|\d{4}[-/.]\d{1,2})\s*$"
)
_NUMBER_RE = re.compile(r"(?<![\w.:@$#])\d+(?:\.\d+)?(?![\w.])")
_TABLE_RE = re.compile(
    r"\b(?:from|join)\s+([A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*){0,2}|\[[^\]]+\](?:\.\[[^\]]+\])*)",
    re.IGNORECASE,
)
_ALLOWED_NUMBER_PREV = ("limit", "top", "offset", "fetch", "first", "next")


@dataclass
class SqlReport:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    params_used: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    normalized: str = ""
    hardcoded: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors


def _segments(sql: str):
    """Split into ('code'|'string'|'ident', text) segments; also yields ('comment', text)
    for -- and /* */ occurring outside literals."""
    i, n = 0, len(sql)
    buf = []
    out = []

    def flush():
        if buf:
            out.append(("code", "".join(buf)))
            buf.clear()

    while i < n:
        c = sql[i]
        if c == "'":
            flush()
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(("string", sql[i:j + 1]))
            i = j + 1
        elif c in '"`':
            flush()
            j = sql.find(c, i + 1)
            j = n - 1 if j < 0 else j
            out.append(("ident", sql[i:j + 1]))
            i = j + 1
        elif c == "[":
            flush()
            j = sql.find("]", i + 1)
            j = n - 1 if j < 0 else j
            out.append(("ident", sql[i:j + 1]))
            i = j + 1
        elif c == "-" and sql.startswith("--", i):
            flush()
            j = sql.find("\n", i)
            j = n if j < 0 else j
            out.append(("comment", sql[i:j]))
            i = j
        elif c == "/" and sql.startswith("/*", i):
            flush()
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(("comment", sql[i:j]))
            i = j
        elif c == "*" and sql.startswith("*/", i):
            # stray comment terminator: treat as a comment marker too (hiding attempt)
            flush()
            out.append(("comment", "*/"))
            i += 2
        else:
            buf.append(c)
            i += 1
    flush()
    return out


def normalize_sql(sql: str) -> str:
    """Whitespace-insensitive canonical form (outside literals); trailing ';' removed."""
    parts = []
    for kind, text in _segments(sql or ""):
        if kind == "code":
            parts.append(re.sub(r"\s+", " ", text))
        elif kind == "comment":
            continue
        else:
            parts.append(text)
    s = "".join(parts).strip()
    while s.endswith(";"):
        s = s[:-1].rstrip()
    return s


def sql_hash(sql: str) -> str:
    return hashlib.sha256(normalize_sql(sql).encode("utf-8")).hexdigest()


def content_hash(sql: str, params: dict | None) -> str:
    """What a human approval covers: the SQL text and its parameter declaration."""
    canon = json.dumps(params or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256((normalize_sql(sql) + "\n" + canon).encode("utf-8")).hexdigest()


def check(sql: str, declared_params=None) -> SqlReport:
    rep = SqlReport()
    if not isinstance(sql, str) or not sql.strip():
        rep.errors.append("empty_sql")
        return rep
    segs = _segments(sql)

    if any(k == "comment" for k, _ in segs):
        rep.errors.append("comments_not_allowed: remove -- and /* */ (they can hide statements)")
    if "\x00" in sql:
        rep.errors.append("nul_byte")

    code_only = "".join(t if k == "code" else " " for k, t in segs)
    stripped = code_only.strip()
    # statement separator: only one trailing ';' is tolerated
    body = stripped
    while body.endswith(";"):
        body = body[:-1].rstrip()
    if ";" in body:
        rep.errors.append("multiple_statements: ';' is only allowed at the very end")
    first = re.match(r"\(*\s*([A-Za-z_]+)", body)
    if not first or first.group(1).lower() not in ALLOWED_FIRST:
        rep.errors.append("not_a_read_query: only SELECT / WITH statements are allowed")
    words = [w.lower() for w in re.findall(r"[A-Za-z_]+", body)]
    bad = sorted({w for w in words if w in FORBIDDEN_WORDS})
    if bad:
        rep.errors.append("forbidden_keyword: " + ", ".join(bad))

    rep.normalized = normalize_sql(sql)

    used = []
    for m in _PARAM_RE.finditer(code_only):
        if m.group(1) not in used:
            used.append(m.group(1))
    rep.params_used = used
    declared = set(declared_params or [])
    undeclared = [p for p in used if p not in declared]
    unused = sorted(declared - set(used))
    if undeclared:
        rep.warnings.append("undeclared_parameter: " + ", ".join(undeclared))
    if unused:
        rep.warnings.append("unused_parameter: " + ", ".join(unused))
    if re.search(r"(?<![\w:])\?", code_only) or re.search(r"(?<![\w@])@[A-Za-z_]\w*", code_only):
        rep.warnings.append("use_named_parameters: write bind variables as :name")

    # hard-coded literals -> recommend parameterisation
    hard = []
    for kind, text in segs:
        if kind == "string" and _DATE_LITERAL_RE.match(text[1:-1]):
            hard.append("date literal %s" % text)
    prev_word = ""
    for kind, text in segs:
        if kind != "code":
            continue
        pos = 0
        for m in _NUMBER_RE.finditer(text):
            before = text[:m.start()]
            pw = re.findall(r"[A-Za-z_]+", before[-12:])
            prev = pw[-1].lower() if pw else prev_word
            tok = m.group(0)
            if tok in ("0", "1") or prev in _ALLOWED_NUMBER_PREV:
                continue
            hard.append("number literal %s" % tok)
        tail = re.findall(r"[A-Za-z_]+", text)
        if tail:
            prev_word = tail[-1].lower()
    if hard:
        rep.hardcoded = True
        rep.warnings.append("hardcoded_literal: " + "; ".join(hard[:5]) +
                            " -- parameterization recommended")

    tables = []
    for m in _TABLE_RE.finditer(code_only):
        t = m.group(1)
        if t.lower() not in tables:
            tables.append(t.lower())
    rep.tables = tables
    return rep
