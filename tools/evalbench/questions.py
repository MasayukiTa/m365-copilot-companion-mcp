"""Question-set format (JSONL), loading, validation and the question template."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .normalize import CompareConfig

ENV_DIR = "MCP_EVALBENCH_DIR"
SPLITS = ("dev", "test")


@dataclass
class Question:
    id: str
    question: str
    split: str = "dev"
    tags: list[str] = field(default_factory=list)
    expected: dict = field(default_factory=dict)
    ambiguous_terms: list[str] = field(default_factory=list)
    assumption_cues: list[str] = field(default_factory=list)
    compare: dict = field(default_factory=dict)
    notes: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def expected_sql(self) -> Optional[str]:
        s = (self.expected or {}).get("sql")
        return s if isinstance(s, str) and s.strip() else None

    @property
    def result_checks(self) -> dict:
        return (self.expected or {}).get("result_checks") or {}

    def compare_config(self) -> CompareConfig:
        return CompareConfig.from_dict(self.compare or (self.expected or {}).get("compare"))

    def content_hash(self) -> str:
        """Hash of everything that affects scoring or what the system is asked (not notes/tags)."""
        payload = {
            "question": self.question,
            "split": self.split,
            "expected": self.expected,
            "ambiguous_terms": self.ambiguous_terms,
            "assumption_cues": self.assumption_cues,
            "compare": self.compare,
        }
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def resolve_path(p: str) -> Path:
    """Relative paths that do not exist are looked up under ``MCP_EVALBENCH_DIR``."""
    path = Path(p)
    if not path.is_absolute() and not path.exists():
        base = os.environ.get(ENV_DIR)
        if base and (Path(base) / path).exists():
            return Path(base) / path
    return path


def _terms(v: Any) -> list[str]:
    out = []
    for t in v or []:
        out.append(str(t.get("term", "")) if isinstance(t, dict) else str(t))
    return [t for t in out if t]


def parse_question(d: dict) -> Question:
    q = Question(
        id=str(d.get("id", "")).strip(),
        question=str(d.get("question", "")).strip(),
        split=str(d.get("split", "dev")).strip().lower(),
        tags=[str(t) for t in d.get("tags") or []],
        expected=d.get("expected") or {},
        ambiguous_terms=_terms(d.get("ambiguous_terms")),
        assumption_cues=[str(c) for c in d.get("assumption_cues") or []],
        compare=d.get("compare") or {},
        notes=str(d.get("notes", "")),
        raw=d,
    )
    return q


def validate_question(q: Question) -> list[str]:
    errs = []
    if not q.id:
        errs.append("missing id")
    if not q.question:
        errs.append("missing question text")
    if q.split not in SPLITS:
        errs.append("split must be 'dev' or 'test', got %r" % q.split)
    if not isinstance(q.expected, dict):
        errs.append("expected must be an object")
        return errs
    rc = q.result_checks
    if not q.expected_sql and not rc:
        errs.append("expected needs 'sql' or 'result_checks' (nothing to grade against)")
    if rc:
        if not isinstance(rc, dict):
            errs.append("result_checks must be an object")
        else:
            for v in rc.get("values") or []:
                if not isinstance(v, dict) or "col" not in v or "equals" not in v:
                    errs.append("result_checks.values entries need 'col' and 'equals'")
                    break
            rcnt = rc.get("row_count")
            if rcnt is not None and not isinstance(rcnt, dict):
                errs.append("result_checks.row_count must be {min,max}")
    try:
        q.compare_config()
    except (ValueError, TypeError) as e:
        errs.append(str(e))
    return errs


def load_questions(path: str | Path) -> tuple[list[Question], list[str]]:
    """Return (questions, problems). Problems are line-level parse errors; duplicates are left for ``check``."""
    p = resolve_path(str(path))
    qs: list[Question] = []
    problems: list[str] = []
    with open(p, "r", encoding="utf-8-sig") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                d = json.loads(line)
                if not isinstance(d, dict):
                    raise ValueError("not an object")
            except ValueError as e:
                problems.append("line %d: invalid JSON (%s)" % (n, e))
                continue
            qs.append(parse_question(d))
    return qs, problems


def template(qid: str = "q001", split: str = "dev") -> dict:
    return {
        "id": qid,
        "question": "Replace with the question exactly as a user would ask it",
        "split": split,
        "tags": [],
        "ambiguous_terms": [],
        "expected": {
            "sql": "SELECT 1 AS example_value",
            "result_checks": {
                "row_count": {"min": 1, "max": 1},
                "columns": ["example_value"],
                "values": [{"col": "example_value", "equals": 1, "tol": 0.0}],
                "must_include_rows": [],
            },
        },
        "compare": {"order": "ignore", "columns": "exact", "rows": "exact", "tol": 1e-6},
        "notes": "",
    }
