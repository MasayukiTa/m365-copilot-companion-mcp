"""SQLite store for the confirmed-SQL library.

Lifecycle (see README):

    draft --(auto: N matching successful runs)--> validated --(any person's OK)--> certified
      ^                                              |                                |
      +------------ SQL edited (hash changes) -------+--------------------------------+
    any state --(NG feedback)--> needs_review flag (+ falls back to draft);  deprecate -> deprecated

"validated" means the SQL ran, returned rows, and a re-run gave the same result. It does NOT
mean the business logic is right. "certified" is an optional human OK; nothing requires it.

The store location comes only from the MCP_SQLLIB_DB environment variable. There is no default
location on purpose: SQL accumulated here can describe private schemas and must never land in
a default, shared place by accident.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import sqlcheck
from .textnorm import normalize

ENV_DB = "MCP_SQLLIB_DB"
ENV_VALIDATE_N = "MCP_SQLLIB_VALIDATE_N"
DEFAULT_VALIDATE_N = 2

STATUSES = ("draft", "validated", "certified", "deprecated")
OUTCOMES = ("ok", "error", "edited")
VERDICTS = ("ok", "ng")
SOURCES = ("user", "auto")

DISCLAIMER = ("Automatic validation only means the SQL ran, returned rows, and gave the same "
              "result when re-run. It is not a guarantee that the business logic is correct.")


class SqllibNotConfigured(RuntimeError):
    """MCP_SQLLIB_DB is not set."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path() -> Path:
    raw = os.environ.get(ENV_DB, "").strip()
    if not raw:
        raise SqllibNotConfigured(
            "%s is not set. The verified-SQL library has no default location by design; "
            "set it to the file where this installation should keep its SQL examples." % ENV_DB)
    return Path(raw)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    question_norm TEXT NOT NULL,
    sql TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    tables_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','validated','certified','deprecated')),
    created_at TEXT NOT NULL,
    created_by TEXT,
    certified_by TEXT,
    certified_at TEXT,
    validated_at TEXT,
    notes TEXT,
    use_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    sql_hash TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    confirmed_hash TEXT,
    needs_review INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS uses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('ok','error','edited')),
    rowcount INTEGER,
    ms INTEGER,
    content_hash TEXT,
    result_hash TEXT,
    args_hash TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    verdict TEXT NOT NULL,
    source TEXT NOT NULL,
    note TEXT,
    content_hash TEXT
);
CREATE TABLE IF NOT EXISTS entries_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id INTEGER NOT NULL,
    archived_at TEXT NOT NULL,
    reason TEXT,
    question TEXT,
    sql TEXT,
    params_json TEXT,
    tables_json TEXT,
    status TEXT,
    sql_hash TEXT,
    content_hash TEXT,
    certified_by TEXT,
    certified_at TEXT,
    validated_at TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_uses_entry ON uses(entry_id);
CREATE INDEX IF NOT EXISTS idx_entries_status ON entries(status);
"""


def connect() -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


# ---------------------------------------------------------------- helpers

def normalize_params(params) -> dict:
    """Accept dict / list of names / JSON string / None -> {name: declaration}."""
    if params is None or params == "":
        return {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except ValueError:
            params = [p.strip() for p in params.split(",") if p.strip()]
    if isinstance(params, (list, tuple)):
        return {str(p).lstrip(":"): {} for p in params}
    if isinstance(params, dict):
        return {str(k).lstrip(":"): v for k, v in params.items()}
    raise ValueError("params must be a dict, a list of names, or a JSON string")


def normalize_list(value) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
            value = loaded if isinstance(loaded, list) else [value]
        except ValueError:
            value = [v.strip() for v in value.split(",") if v.strip()]
    return [str(v) for v in value]


def effective_status(row) -> str:
    """Stored status, except that a validated/certified entry whose current content no longer
    matches what was confirmed (edited behind the API's back) counts as draft."""
    st = row["status"]
    if st in ("validated", "certified") and row["confirmed_hash"] != row["content_hash"]:
        return "draft"
    return st


def row_to_dict(row) -> dict:
    d = dict(row)
    d["params"] = json.loads(d.pop("params_json") or "{}")
    d["tables"] = json.loads(d.pop("tables_json") or "[]")
    d["stored_status"] = d["status"]
    d["status"] = effective_status(row)
    d["needs_review"] = bool(d["needs_review"])
    return d


def _archive(con, row, reason: str) -> None:
    con.execute(
        "INSERT INTO entries_history (entry_id, archived_at, reason, question, sql, params_json,"
        " tables_json, status, sql_hash, content_hash, certified_by, certified_at, validated_at,"
        " notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (row["id"], _now(), reason, row["question"], row["sql"], row["params_json"],
         row["tables_json"], row["status"], row["sql_hash"], row["content_hash"],
         row["certified_by"], row["certified_at"], row["validated_at"], row["notes"]))


def _get_row(con, entry_id: int):
    row = con.execute("SELECT * FROM entries WHERE id=?", (int(entry_id),)).fetchone()
    if row is None:
        raise KeyError("no entry with id %s" % entry_id)
    return row


def _add_note(notes, line: str) -> str:
    notes = notes or ""
    return (notes + "\n" + line).strip() if line not in notes else notes


RECOMMEND_NOTE = "パラメータ化の推奨 / parameterization recommended"


# ---------------------------------------------------------------- writes

def save(question: str, sql: str, params=None, tables=None, notes=None,
         created_by: str = "mcp-agent") -> dict:
    """Store a new example. ALWAYS as draft: there is no status argument."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question is empty")
    p = normalize_params(params)
    rep = sqlcheck.check(sql, p)
    if not rep.ok:
        raise ValueError("SQL rejected: " + "; ".join(rep.errors))
    tbl = normalize_list(tables) or rep.tables
    qn = normalize(question)
    shash = sqlcheck.sql_hash(sql)
    chash = sqlcheck.content_hash(sql, p)
    with closing(connect()) as con:
        dup = con.execute(
            "SELECT * FROM entries WHERE question_norm=? AND sql_hash=? AND status!='deprecated'",
            (qn, shash)).fetchone()
        if dup is not None:
            return {"id": dup["id"], "status": effective_status(dup), "duplicate": True,
                    "warnings": rep.warnings}
        note = notes or ""
        if rep.hardcoded:
            note = _add_note(note, RECOMMEND_NOTE)
        cur = con.execute(
            "INSERT INTO entries (question, question_norm, sql, params_json, tables_json, status,"
            " created_at, created_by, notes, sql_hash, content_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (question.strip(), qn, sql.strip(), json.dumps(p, ensure_ascii=False),
             json.dumps(tbl, ensure_ascii=False), "draft", _now(), created_by, note or None,
             shash, chash))
        con.commit()
        return {"id": cur.lastrowid, "status": "draft", "duplicate": False,
                "warnings": rep.warnings}


def update_entry(entry_id: int, sql=None, question=None, params=None, tables=None,
                 notes=None) -> dict:
    """Edit an entry (operator path). If the SQL or its parameter declaration changes, a
    validated/certified entry goes back to draft and the old version is kept in history."""
    with closing(connect()) as con:
        row = _get_row(con, entry_id)
        new_sql = row["sql"] if sql is None else sql
        new_params = json.loads(row["params_json"]) if params is None else normalize_params(params)
        rep = sqlcheck.check(new_sql, new_params)
        if not rep.ok:
            raise ValueError("SQL rejected: " + "; ".join(rep.errors))
        new_hash = sqlcheck.content_hash(new_sql, new_params)
        changed = new_hash != row["content_hash"]
        status = row["status"]
        demoted = False
        if changed:
            _archive(con, row, "content edited")
            if status in ("validated", "certified"):
                status, demoted = "draft", True
        q = row["question"] if question is None else question.strip()
        tbl = (json.loads(row["tables_json"]) if tables is None and not changed
               else (normalize_list(tables) or rep.tables))
        note = row["notes"] if notes is None else notes
        if changed and rep.hardcoded:
            note = _add_note(note, RECOMMEND_NOTE)
        con.execute(
            "UPDATE entries SET question=?, question_norm=?, sql=?, params_json=?, tables_json=?,"
            " status=?, notes=?, sql_hash=?, content_hash=?, confirmed_hash=?,"
            " certified_by=?, certified_at=?, validated_at=? WHERE id=?",
            (q, normalize(q), new_sql.strip(), json.dumps(new_params, ensure_ascii=False),
             json.dumps(tbl, ensure_ascii=False), status, note, sqlcheck.sql_hash(new_sql),
             new_hash,
             None if demoted else row["confirmed_hash"],
             None if demoted else row["certified_by"], None if demoted else row["certified_at"],
             None if demoted else row["validated_at"], int(entry_id)))
        con.commit()
        return {"id": int(entry_id), "changed": changed, "status": status, "demoted": demoted}


def _qualifying(r) -> bool:
    return (r["outcome"] == "ok" and r["rowcount"] is not None and r["rowcount"] > 0
            and bool(r["result_hash"]))


def _streak(con, entry_id: int, content_hash: str) -> int:
    rows = con.execute(
        "SELECT outcome, rowcount, result_hash, args_hash FROM uses"
        " WHERE entry_id=? AND content_hash=? ORDER BY id DESC", (entry_id, content_hash)).fetchall()
    if not rows or not _qualifying(rows[0]):
        return 0
    ref, n = rows[0], 0
    for r in rows:
        if r["args_hash"] != ref["args_hash"]:
            continue
        if not _qualifying(r) or r["result_hash"] != ref["result_hash"]:
            break
        n += 1
    return n


def validate_threshold() -> int:
    try:
        return max(1, int(os.environ.get(ENV_VALIDATE_N, DEFAULT_VALIDATE_N)))
    except ValueError:
        return DEFAULT_VALIDATE_N


def record_use(entry_id: int, outcome: str, rowcount=None, ms=None, result_hash=None,
               args_hash=None, validate_after=None) -> dict:
    """Record one execution. A draft is promoted to validated automatically once the latest
    `validate_after` (default 2) executions of the current SQL, with the same arguments, all
    succeeded, returned rows, and produced the same result fingerprint."""
    if outcome not in OUTCOMES:
        raise ValueError("outcome must be one of %s" % (OUTCOMES,))
    need = validate_after if validate_after else validate_threshold()
    with closing(connect()) as con:
        row = _get_row(con, entry_id)
        con.execute(
            "INSERT INTO uses (entry_id, at, outcome, rowcount, ms, content_hash, result_hash,"
            " args_hash) VALUES (?,?,?,?,?,?,?,?)",
            (row["id"], _now(), outcome, rowcount, ms, row["content_hash"],
             (result_hash or None), (args_hash or "")))
        con.execute("UPDATE entries SET use_count=use_count+1, last_used_at=? WHERE id=?",
                    (_now(), row["id"]))
        streak = _streak(con, row["id"], row["content_hash"])
        promoted = False
        status = effective_status(row)
        if status == "draft" and row["status"] != "deprecated" and streak >= need:
            con.execute("UPDATE entries SET status='validated', validated_at=?, confirmed_hash=?"
                        " WHERE id=?", (_now(), row["content_hash"], row["id"]))
            status, promoted = "validated", True
        con.commit()
        return {"id": row["id"], "status": status, "promoted": promoted,
                "matching_runs": streak, "runs_needed": need}


def feedback(entry_id: int, verdict: str, note=None, source: str = "user") -> dict:
    """Light feedback. ok from a user certifies; ng flags the entry for review and takes it
    out of the trusted tiers (it returns to draft)."""
    if verdict not in VERDICTS:
        raise ValueError("verdict must be 'ok' or 'ng'")
    if source not in SOURCES:
        raise ValueError("source must be 'user' or 'auto'")
    with closing(connect()) as con:
        row = _get_row(con, entry_id)
        con.execute("INSERT INTO feedback (entry_id, at, verdict, source, note, content_hash)"
                    " VALUES (?,?,?,?,?,?)",
                    (row["id"], _now(), verdict, source, note, row["content_hash"]))
        status = effective_status(row)
        needs = row["needs_review"]
        if row["status"] == "deprecated":
            pass
        elif verdict == "ng":
            needs = 1
            if status in ("validated", "certified"):
                con.execute("UPDATE entries SET status='draft', confirmed_hash=NULL,"
                            " certified_by=NULL, certified_at=NULL, validated_at=NULL WHERE id=?",
                            (row["id"],))
                status = "draft"
        elif source == "user":
            con.execute("UPDATE entries SET status='certified', confirmed_hash=?, certified_by=?,"
                        " certified_at=?, needs_review=0 WHERE id=?",
                        (row["content_hash"], "user", _now(), row["id"]))
            status, needs = "certified", 0
        con.execute("UPDATE entries SET needs_review=? WHERE id=?", (needs, row["id"]))
        con.commit()
        return {"id": row["id"], "status": status, "needs_review": bool(needs)}


def certify(entry_id: int, by: str) -> dict:
    if not by or not by.strip():
        raise ValueError("certify needs a name (--by)")
    with closing(connect()) as con:
        row = _get_row(con, entry_id)
        if row["status"] == "deprecated":
            raise ValueError("entry %s is deprecated" % entry_id)
        con.execute("UPDATE entries SET status='certified', confirmed_hash=?, certified_by=?,"
                    " certified_at=?, needs_review=0 WHERE id=?",
                    (row["content_hash"], by.strip(), _now(), row["id"]))
        con.commit()
        return {"id": row["id"], "status": "certified", "certified_by": by.strip()}


def deprecate(entry_id: int, note=None) -> dict:
    with closing(connect()) as con:
        row = _get_row(con, entry_id)
        _archive(con, row, "deprecated")
        con.execute("UPDATE entries SET status='deprecated', notes=? WHERE id=?",
                    (_add_note(row["notes"], note) if note else row["notes"], row["id"]))
        con.commit()
        return {"id": row["id"], "status": "deprecated"}


# ---------------------------------------------------------------- reads

def get(entry_id: int) -> dict:
    with closing(connect()) as con:
        return row_to_dict(_get_row(con, entry_id))


def list_entries(status: str | None = None) -> list:
    with closing(connect()) as con:
        rows = con.execute("SELECT * FROM entries ORDER BY id").fetchall()
    out = [row_to_dict(r) for r in rows]
    return [e for e in out if status is None or e["status"] == status]


def history(entry_id: int) -> list:
    with closing(connect()) as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM entries_history WHERE entry_id=? ORDER BY history_id", (int(entry_id),))]


def uses(entry_id: int) -> list:
    with closing(connect()) as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM uses WHERE entry_id=? ORDER BY id", (int(entry_id),))]


# ---------------------------------------------------------------- import / export

def export_jsonl(path, status: str | None = None) -> int:
    entries = list_entries(status)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for e in entries:
            rec = {k: e[k] for k in ("id", "question", "sql", "params", "tables", "status",
                                     "notes", "created_by", "certified_by", "certified_at")}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return len(entries)


def import_jsonl(path, trust_status: bool = False, by: str | None = None) -> dict:
    """Import records. Every record becomes a draft unless trust_status is set (operator
    flag), in which case validated/certified/deprecated are kept."""
    added = skipped = 0
    errors = []
    with open(path, "r", encoding="utf-8-sig") as fh:
        for ln, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                res = save(rec["question"], rec["sql"], rec.get("params"), rec.get("tables"),
                           rec.get("notes"), created_by=by or rec.get("created_by") or "import")
                if res["duplicate"]:
                    skipped += 1
                    continue
                added += 1
                want = rec.get("status")
                if trust_status and want in ("validated", "certified", "deprecated"):
                    with closing(connect()) as con:
                        row = _get_row(con, res["id"])
                        if want == "deprecated":
                            con.execute("UPDATE entries SET status='deprecated' WHERE id=?",
                                        (res["id"],))
                        else:
                            con.execute(
                                "UPDATE entries SET status=?, confirmed_hash=?, validated_at=?,"
                                " certified_by=?, certified_at=? WHERE id=?",
                                (want, row["content_hash"], _now(),
                                 (rec.get("certified_by") or by) if want == "certified" else None,
                                 _now() if want == "certified" else None, res["id"]))
                        con.commit()
            except Exception as exc:  # one bad line must not stop the file
                errors.append("line %d: %s" % (ln, exc))
    return {"added": added, "skipped_duplicates": skipped, "errors": errors}
