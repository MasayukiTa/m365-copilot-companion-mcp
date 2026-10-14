"""The discovery store: a small SQLite file holding profiles, candidates and verifications.

The caller chooses the path; nothing is written anywhere else. Keys include the column's
sample hash, so a re-run only redoes work for columns whose sampled content changed.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional

from .profile import ColumnProfile

_SCHEMA = """
CREATE TABLE IF NOT EXISTS column_profile (
    table_name TEXT NOT NULL, column_name TEXT NOT NULL, col_type TEXT NOT NULL,
    sample_hash TEXT NOT NULL, profile_json TEXT NOT NULL, updated_at REAL NOT NULL,
    PRIMARY KEY (table_name, column_name)
);
CREATE TABLE IF NOT EXISTS candidate (
    a_table TEXT NOT NULL, a_column TEXT NOT NULL, b_table TEXT NOT NULL, b_column TEXT NOT NULL,
    score REAL NOT NULL, reasons_json TEXT NOT NULL, updated_at REAL NOT NULL,
    PRIMARY KEY (a_table, a_column, b_table, b_column)
);
CREATE TABLE IF NOT EXISTS verification (
    a_table TEXT NOT NULL, a_column TEXT NOT NULL, b_table TEXT NOT NULL, b_column TEXT NOT NULL,
    a_hash TEXT NOT NULL, b_hash TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
    confidence REAL NOT NULL, evidence_json TEXT NOT NULL, updated_at REAL NOT NULL,
    PRIMARY KEY (a_table, a_column, b_table, b_column)
);
"""


class DiscoveryStore:
    def __init__(self, path: str) -> None:
        self.path = str(path)
        self._con = sqlite3.connect(self.path)
        self._con.executescript(_SCHEMA)

    # profiles
    def get_profile(self, table: str, column: str) -> Optional[ColumnProfile]:
        row = self._con.execute(
            "SELECT profile_json FROM column_profile WHERE table_name=? AND column_name=?",
            (table, column)).fetchone()
        return ColumnProfile.from_dict(json.loads(row[0])) if row else None

    def put_profile(self, prof: ColumnProfile) -> None:
        self._con.execute(
            "INSERT OR REPLACE INTO column_profile VALUES (?,?,?,?,?,?)",
            (prof.table, prof.column, prof.declared_type, prof.sample_hash,
             json.dumps(prof.to_dict(), ensure_ascii=False, default=str), time.time()))

    def all_profiles(self) -> list[ColumnProfile]:
        rows = self._con.execute("SELECT profile_json FROM column_profile ORDER BY table_name, column_name")
        return [ColumnProfile.from_dict(json.loads(r[0])) for r in rows]

    # candidates
    def put_candidates(self, cands) -> None:
        now = time.time()
        self._con.executemany(
            "INSERT OR REPLACE INTO candidate VALUES (?,?,?,?,?,?,?)",
            [(c.a_table, c.a_column, c.b_table, c.b_column, c.score,
              json.dumps(c.reasons, ensure_ascii=False), now) for c in cands])

    def candidate_count(self) -> int:
        return self._con.execute("SELECT COUNT(*) FROM candidate").fetchone()[0]

    # verifications
    def get_verification(self, a_table: str, a_column: str, b_table: str, b_column: str) -> Optional[dict]:
        row = self._con.execute(
            "SELECT a_hash,b_hash,status,reason,confidence,evidence_json FROM verification "
            "WHERE a_table=? AND a_column=? AND b_table=? AND b_column=?",
            (a_table, a_column, b_table, b_column)).fetchone()
        if not row:
            return None
        return {"a_hash": row[0], "b_hash": row[1], "status": row[2], "reason": row[3],
                "confidence": row[4], "evidence": json.loads(row[5])}

    def put_verification(self, a_table: str, a_column: str, b_table: str, b_column: str,
                         a_hash: str, b_hash: str, status: str, reason: str,
                         confidence: float, evidence: dict[str, Any]) -> None:
        self._con.execute(
            "INSERT OR REPLACE INTO verification VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (a_table, a_column, b_table, b_column, a_hash, b_hash, status, reason,
             float(confidence), json.dumps(evidence, ensure_ascii=False, default=str), time.time()))

    def top_joins(self, limit: int = 20, include_unverified: bool = False) -> list[dict]:
        where = "" if include_unverified else "WHERE status = 'verified'"
        rows = self._con.execute(
            f"SELECT a_table,a_column,b_table,b_column,status,reason,confidence,evidence_json "
            f"FROM verification {where} ORDER BY confidence DESC, a_table, a_column, b_table, b_column LIMIT ?",
            (limit,)).fetchall()
        return [{"a_table": r[0], "a_column": r[1], "b_table": r[2], "b_column": r[3],
                 "status": r[4], "reason": r[5], "confidence": r[6], "evidence": json.loads(r[7])}
                for r in rows]

    def commit(self) -> None:
        self._con.commit()

    def close(self) -> None:
        self._con.commit()
        self._con.close()
