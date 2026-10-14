"""Answer ingestion: JSONL answers plus helpers to import pasted CSV / JSON results."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .normalize import Result
from .questions import resolve_path


@dataclass
class Answer:
    id: str
    system: str = "default"
    run: int = 1
    sql: Optional[str] = None
    result: Optional[Result] = None
    interpretation: str = ""
    text: str = ""
    seconds: Optional[float] = None
    error: Optional[str] = None
    clarified: bool = False

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "id": self.id,
            "system": self.system,
            "run": self.run,
            "sql": self.sql,
            "result": self.result.to_dict() if self.result else None,
            "interpretation": self.interpretation,
            "seconds": self.seconds,
            "error": self.error,
            "clarified": self.clarified,
        }
        if self.text:
            d["text"] = self.text
        return d


def parse_answer(d: dict) -> Answer:
    res = d.get("result")
    seconds = d.get("seconds")
    return Answer(
        id=str(d.get("id", "")).strip(),
        system=str(d.get("system") or "default").strip(),
        run=int(d.get("run") or 1),
        sql=d.get("sql") or None,
        result=Result.from_dict(res) if res else None,
        interpretation=str(d.get("interpretation") or ""),
        text=str(d.get("text") or d.get("message") or ""),
        seconds=float(seconds) if seconds not in (None, "") else None,
        error=(str(d["error"]) if d.get("error") else None),
        clarified=bool(d.get("clarified", False)),
    )


def load_answers(path: str | Path) -> tuple[list[Answer], list[str]]:
    p = resolve_path(str(path))
    out: list[Answer] = []
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
                out.append(parse_answer(d))
            except (ValueError, TypeError) as e:
                problems.append("line %d: %s" % (n, e))
    return out, problems


def append_answer(path: str | Path, ans: Answer) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ans.to_dict(), ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ import helpers

def result_from_csv(text: str, delimiter: Optional[str] = None) -> Result:
    """Parse pasted CSV/TSV (first row = header). Delimiter is sniffed when not given."""
    text = text.lstrip("﻿").strip("\r\n")
    if not text.strip():
        return Result([], [])
    if delimiter is None:
        first = text.splitlines()[0]
        delimiter = "\t" if first.count("\t") >= first.count(",") and "\t" in first else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    header, body = rows[0], rows[1:]
    return Result([h.strip() for h in header], [r for r in body if any(c.strip() for c in r)])


def result_from_json(text: str) -> Result:
    """Accept {"columns","rows"}, a list of row objects, or a list of lists with no header."""
    data = json.loads(text)
    if isinstance(data, dict):
        if "rows" in data or "columns" in data:
            return Result.from_dict(data)
        for key in ("data", "result", "results", "value"):
            if key in data:
                return result_from_json(json.dumps(data[key]))
        raise ValueError("JSON object has no 'rows'")
    if isinstance(data, list):
        if not data:
            return Result([], [])
        if isinstance(data[0], dict):
            cols = list(data[0].keys())
            return Result(cols, [[r.get(c) for c in cols] for r in data])
        width = len(data[0])
        return Result(["col%d" % (i + 1) for i in range(width)], [list(r) for r in data])
    raise ValueError("unsupported JSON shape for a result")


def answer_from_paste(
    qid: str,
    system: str,
    *,
    run: int = 1,
    csv_text: Optional[str] = None,
    json_text: Optional[str] = None,
    sql: Optional[str] = None,
    interpretation: str = "",
    text: str = "",
    seconds: Optional[float] = None,
    clarified: bool = False,
    error: Optional[str] = None,
) -> Answer:
    result = None
    if csv_text is not None:
        result = result_from_csv(csv_text)
    elif json_text is not None:
        result = result_from_json(json_text)
    return Answer(id=qid, system=system, run=run, sql=sql, result=result,
                  interpretation=interpretation, text=text, seconds=seconds,
                  error=error, clarified=clarified)
