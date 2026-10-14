# -*- coding: utf-8 -*-
"""Load, validate and cache the semantic-layer files.

The content folder is named by the MCP_SEMANTIC_DIR environment variable and holds only JSON
(UTF-8):

    glossary.json          {"terms":   [ {term, synonyms, meaning, resolves_to, status, caveats} ]}
    metrics.json           {"metrics": [ {name, description, sql, params, dimensions, tables,
                                           synonyms, status, caveats} ]}
    catalog/<any>.json     one table or view per file:
                           {name, kind, description, row_estimate, columns, keys, joins,
                            pitfalls, performance, status}

Rules this module keeps:

* Only ``term`` / ``name`` are required. Every other field may be absent.
* A broken file never stops the others. Problems are collected (file, location, message) and
  returned with the data, so a caller can say exactly which file and which entry is wrong.
* A bad entry is skipped; a bad optional field is dropped with a warning.
* Unknown keys are preserved untouched (forward compatibility).
* No MCP_SEMANTIC_DIR means "nothing configured", which is reported as such. It is never
  silently treated as an empty dictionary that "defines nothing".
* Reloading is driven by file modification time and size, so edits show up without a restart.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._text import as_str_list, norm

ENV_VAR = "MCP_SEMANTIC_DIR"
STATUSES = ("draft", "certified")
RESOLVES_KINDS = ("filter", "column", "table", "metric", "note")
TABLE_KINDS = ("table", "view")
NOT_CONFIGURED_MESSAGE = (
    "%s is not set. The semantic layer needs a folder with glossary.json, metrics.json and "
    "catalog/*.json; point %s at it (outside the repository)." % (ENV_VAR, ENV_VAR))


@dataclass
class Problem:
    file: str
    where: str
    message: str
    level: str = "error"          # "error" (entry/file skipped) | "warning" (field dropped)

    def as_dict(self) -> Dict[str, str]:
        return {"file": self.file, "where": self.where, "message": self.message,
                "level": self.level}


@dataclass
class Store:
    directory: Optional[Path] = None
    configured: bool = False
    exists: bool = False
    terms: List[Dict[str, Any]] = field(default_factory=list)
    metrics: List[Dict[str, Any]] = field(default_factory=list)
    tables: Dict[str, Dict[str, Any]] = field(default_factory=dict)   # key = norm(name)
    problems: List[Problem] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    loaded_at: float = 0.0
    signature: Tuple = ()

    def error(self) -> Optional[str]:
        """A message when the store cannot answer at all, else None."""
        if not self.configured:
            return NOT_CONFIGURED_MESSAGE
        if not self.exists:
            return "%s points at %s, which is not a folder." % (ENV_VAR, self.directory)
        return None

    def problem_dicts(self, level: Optional[str] = None) -> List[Dict[str, str]]:
        return [p.as_dict() for p in self.problems if level is None or p.level == level]

    def status(self) -> Dict[str, Any]:
        return {
            "configured": self.configured,
            "directory": str(self.directory) if self.directory else None,
            "files_read": list(self.files),
            "terms": len(self.terms),
            "metrics": len(self.metrics),
            "tables": len(self.tables),
            "problems": len(self.problems),
        }

    def find_table(self, name: str) -> Optional[Dict[str, Any]]:
        return self.tables.get(norm(name))

    def find_metric(self, name: str) -> Optional[Dict[str, Any]]:
        key = norm(name)
        for m in self.metrics:
            if norm(m["name"]) == key:
                return m
        for m in self.metrics:
            if key in [norm(s) for s in m.get("synonyms", [])]:
                return m
        return None


# ----------------------------------------------------------------------------- reading

def _read_json(path: Path, store: Store, rel: str) -> Any:
    """Parse one file. On any failure record a Problem and return None."""
    try:
        raw = path.read_bytes()
    except OSError as e:
        store.problems.append(Problem(rel, "(file)", "cannot read: %s" % e))
        return None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        store.problems.append(Problem(
            rel, "byte %d" % e.start, "not valid UTF-8 (files must be UTF-8): %s" % e.reason))
        return None
    if not text.strip():
        store.problems.append(Problem(rel, "(file)", "file is empty", "warning"))
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        store.problems.append(Problem(
            rel, "line %d column %d" % (e.lineno, e.colno), "invalid JSON: %s" % e.msg))
        return None


def _status(entry: Dict[str, Any], store: Store, rel: str, where: str) -> str:
    st = entry.get("status")
    if st is None:
        return "draft"
    if isinstance(st, str) and st.strip().lower() in STATUSES:
        return st.strip().lower()
    store.problems.append(Problem(
        rel, where, "status %r is not one of %s; treated as draft" % (st, "|".join(STATUSES)),
        "warning"))
    return "draft"


def _list_field(entry: Dict[str, Any], key: str, store: Store, rel: str, where: str) -> List[str]:
    v = entry.get(key)
    if v is None:
        return []
    if isinstance(v, (str, list, tuple)):
        out = as_str_list(v)
        if isinstance(v, (list, tuple)) and len(out) != len(v):
            store.problems.append(Problem(
                rel, "%s.%s" % (where, key), "non-text items were dropped", "warning"))
        return out
    store.problems.append(Problem(
        rel, "%s.%s" % (where, key), "expected a list of text, got %s; field ignored"
        % type(v).__name__, "warning"))
    return []


def _text_field(entry: Dict[str, Any], key: str, store: Store, rel: str, where: str) -> str:
    v = entry.get(key)
    if v is None:
        return ""
    if isinstance(v, (str, int, float)):
        return str(v)
    store.problems.append(Problem(
        rel, "%s.%s" % (where, key), "expected text, got %s; field ignored"
        % type(v).__name__, "warning"))
    return ""


def _items(data: Any, key: str, store: Store, rel: str) -> List[Tuple[int, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        store.problems.append(Problem(
            rel, "(top level)", 'expected an object like {"%s": [ ... ]}' % key))
        return []
    return list(enumerate(data[key]))


def _load_glossary(path: Path, store: Store) -> None:
    rel = "glossary.json"
    data = _read_json(path, store, rel)
    if data is None:
        return
    seen: Dict[str, int] = {}
    for i, e in _items(data, "terms", store, rel):
        where = "terms[%d]" % i
        if not isinstance(e, dict):
            store.problems.append(Problem(rel, where, "entry is not an object; skipped"))
            continue
        term = e.get("term")
        if not isinstance(term, str) or not term.strip():
            store.problems.append(Problem(rel, where, "missing required 'term'; entry skipped"))
            continue
        out = dict(e)                                  # unknown keys are kept
        out["term"] = term.strip()
        out["synonyms"] = _list_field(e, "synonyms", store, rel, where)
        out["meaning"] = _text_field(e, "meaning", store, rel, where)
        out["caveats"] = _list_field(e, "caveats", store, rel, where)
        out["status"] = _status(e, store, rel, where)
        rt = e.get("resolves_to")
        if rt is None:
            out["resolves_to"] = None
        elif isinstance(rt, dict):
            kind = rt.get("kind")
            if kind is not None and kind not in RESOLVES_KINDS:
                store.problems.append(Problem(
                    rel, where + ".resolves_to.kind",
                    "kind %r is not one of %s (kept as is)" % (kind, "|".join(RESOLVES_KINDS)),
                    "warning"))
            out["resolves_to"] = dict(rt)
        else:
            store.problems.append(Problem(
                rel, where + ".resolves_to", "expected an object; field ignored", "warning"))
            out["resolves_to"] = None
        key = norm(out["term"])
        if key in seen:
            store.problems.append(Problem(
                rel, where, "term %r repeats terms[%d]" % (out["term"], seen[key]), "warning"))
        seen.setdefault(key, i)
        store.terms.append(out)


_PARAM_TYPES = ("date", "int", "float", "string", "bool")


def _load_metrics(path: Path, store: Store) -> None:
    rel = "metrics.json"
    data = _read_json(path, store, rel)
    if data is None:
        return
    seen: Dict[str, int] = {}
    for i, e in _items(data, "metrics", store, rel):
        where = "metrics[%d]" % i
        if not isinstance(e, dict):
            store.problems.append(Problem(rel, where, "entry is not an object; skipped"))
            continue
        name = e.get("name")
        if not isinstance(name, str) or not name.strip():
            store.problems.append(Problem(rel, where, "missing required 'name'; entry skipped"))
            continue
        out = dict(e)
        out["name"] = name.strip()
        out["description"] = _text_field(e, "description", store, rel, where)
        sql = e.get("sql")
        if sql is None:
            store.problems.append(Problem(
                rel, where + ".sql", "metric %r has no sql; it can be found but not run"
                % out["name"], "warning"))
            out["sql"] = ""
        elif isinstance(sql, str):
            out["sql"] = sql
        else:
            store.problems.append(Problem(
                rel, where + ".sql", "expected text, got %s; field ignored" % type(sql).__name__,
                "warning"))
            out["sql"] = ""
        params: List[Dict[str, Any]] = []
        praw = e.get("params")
        if praw is not None and not isinstance(praw, list):
            store.problems.append(Problem(
                rel, where + ".params", "expected a list; field ignored", "warning"))
            praw = []
        for j, p in enumerate(praw or []):
            pw = "%s.params[%d]" % (where, j)
            if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p["name"]:
                store.problems.append(Problem(rel, pw, "needs an object with a 'name'; dropped",
                                              "warning"))
                continue
            q = dict(p)
            t = str(p.get("type", "string")).lower()
            if t not in _PARAM_TYPES:
                store.problems.append(Problem(
                    rel, pw + ".type", "type %r is not one of %s; treated as string"
                    % (p.get("type"), "|".join(_PARAM_TYPES)), "warning"))
                t = "string"
            q["type"] = t
            q.setdefault("default", None)
            params.append(q)
        out["params"] = params
        for k in ("dimensions", "tables", "synonyms", "caveats"):
            out[k] = _list_field(e, k, store, rel, where)
        out["status"] = _status(e, store, rel, where)
        key = norm(out["name"])
        if key in seen:
            store.problems.append(Problem(
                rel, where, "metric %r repeats metrics[%d]" % (out["name"], seen[key]),
                "warning"))
        seen.setdefault(key, i)
        store.metrics.append(out)


def _load_table(entry: Any, store: Store, rel: str, where: str) -> None:
    if not isinstance(entry, dict):
        store.problems.append(Problem(rel, where, "table entry is not an object; skipped"))
        return
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        store.problems.append(Problem(rel, where, "missing required 'name'; table skipped"))
        return
    out = dict(entry)
    out["name"] = name.strip()
    kind = entry.get("kind", "table")
    if kind not in TABLE_KINDS:
        store.problems.append(Problem(
            rel, where + ".kind", "kind %r is not table|view; treated as table" % (kind,),
            "warning"))
        kind = "table"
    out["kind"] = kind
    out["description"] = _text_field(entry, "description", store, rel, where)
    out["performance"] = _text_field(entry, "performance", store, rel, where)
    rows = entry.get("row_estimate")
    if rows is not None and (isinstance(rows, bool) or not isinstance(rows, (int, float))):
        store.problems.append(Problem(
            rel, where + ".row_estimate", "expected a number; field ignored", "warning"))
        rows = None
    out["row_estimate"] = rows
    out["keys"] = _list_field(entry, "keys", store, rel, where)
    out["pitfalls"] = _list_field(entry, "pitfalls", store, rel, where)
    out["status"] = _status(entry, store, rel, where)

    cols: List[Dict[str, Any]] = []
    craw = entry.get("columns")
    if craw is not None and not isinstance(craw, list):
        store.problems.append(Problem(rel, where + ".columns", "expected a list; field ignored",
                                      "warning"))
        craw = []
    for j, c in enumerate(craw or []):
        cw = "%s.columns[%d]" % (where, j)
        if not isinstance(c, dict) or not isinstance(c.get("name"), str) or not c["name"].strip():
            store.problems.append(Problem(rel, cw, "column needs an object with a 'name'; dropped",
                                          "warning"))
            continue
        cc = dict(c)
        cc["name"] = c["name"].strip()
        cc["type"] = _text_field(c, "type", store, rel, cw)
        cc["meaning"] = _text_field(c, "meaning", store, rel, cw)
        cc["notes"] = _text_field(c, "notes", store, rel, cw)
        vals = c.get("values")
        if vals is None:
            cc["values"] = {}
        elif isinstance(vals, dict):
            cc["values"] = {str(k): ("" if v is None else str(v)) for k, v in vals.items()}
        else:
            store.problems.append(Problem(
                rel, cw + ".values", "expected an object {value: meaning}; field ignored",
                "warning"))
            cc["values"] = {}
        cols.append(cc)
    out["columns"] = cols

    joins: List[Dict[str, Any]] = []
    jraw = entry.get("joins")
    if jraw is not None and not isinstance(jraw, list):
        store.problems.append(Problem(rel, where + ".joins", "expected a list; field ignored",
                                      "warning"))
        jraw = []
    for j, jn in enumerate(jraw or []):
        jw = "%s.joins[%d]" % (where, j)
        if not isinstance(jn, dict) or not isinstance(jn.get("to"), str) or not jn["to"].strip():
            store.problems.append(Problem(rel, jw, "join needs an object with a 'to'; dropped",
                                          "warning"))
            continue
        jj = dict(jn)
        jj["to"] = jn["to"].strip()
        jj["on"] = _text_field(jn, "on", store, rel, jw)
        jj["note"] = _text_field(jn, "note", store, rel, jw)
        conf = jn.get("confidence")
        if conf is not None and (isinstance(conf, bool) or not isinstance(conf, (int, float))):
            store.problems.append(Problem(
                rel, jw + ".confidence", "expected a number 0..1; field ignored", "warning"))
            conf = None
        jj["confidence"] = conf
        joins.append(jj)
    out["joins"] = joins

    key = norm(out["name"])
    if key in store.tables:
        store.problems.append(Problem(
            rel, where, "table %r is already defined in another file; this one ignored"
            % out["name"], "warning"))
        return
    out["_file"] = rel
    store.tables[key] = out


def _load_catalog(directory: Path, store: Store) -> None:
    cdir = directory / "catalog"
    if not cdir.is_dir():
        return
    for p in sorted(cdir.glob("*.json")):
        rel = "catalog/" + p.name
        store.files.append(rel)
        data = _read_json(p, store, rel)
        if data is None:
            continue
        if isinstance(data, list):                      # tolerated: several tables in one file
            for i, t in enumerate(data):
                _load_table(t, store, rel, "[%d]" % i)
        elif isinstance(data, dict) and isinstance(data.get("tables"), list) \
                and "name" not in data:
            for i, t in enumerate(data["tables"]):
                _load_table(t, store, rel, "tables[%d]" % i)
        else:
            _load_table(data, store, rel, "(top level)")


# ------------------------------------------------------------------------ cache + entry

def _signature(directory: Path) -> Tuple:
    sig = []
    names = ["glossary.json", "metrics.json"]
    cdir = directory / "catalog"
    try:
        cat = sorted("catalog/" + p.name for p in cdir.glob("*.json")) if cdir.is_dir() else []
    except OSError:
        cat = []
    for rel in names + cat:
        try:
            st = (directory / rel).stat()
            sig.append((rel, st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((rel, None, None))
    return tuple(sig)


def load_directory(directory: Path) -> Store:
    """Read every file now (no cache). Never raises for content problems."""
    store = Store(directory=directory, configured=True, exists=directory.is_dir())
    if not store.exists:
        return store
    for fname, loader in (("glossary.json", _load_glossary), ("metrics.json", _load_metrics)):
        p = directory / fname
        if p.is_file():
            store.files.append(fname)
            loader(p, store)
    _load_catalog(directory, store)
    store.loaded_at = time.time()
    store.signature = _signature(directory)
    return store


_LOCK = threading.Lock()
_CACHE: Dict[str, Store] = {}


def get_store(directory: Optional[str] = None) -> Store:
    """The current store: from `directory`, else from MCP_SEMANTIC_DIR; cached by mtime/size."""
    raw = directory if directory is not None else os.environ.get(ENV_VAR, "")
    if not raw or not str(raw).strip():
        return Store()                                  # configured=False
    path = Path(str(raw).strip()).expanduser()
    key = str(path)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached.exists and path.is_dir() \
                and cached.signature == _signature(path):
            return cached
        store = load_directory(path)
        _CACHE[key] = store
        return store


