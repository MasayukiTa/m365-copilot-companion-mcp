"""Scoring of one answer: execution, result match, result checks, interpretation, errors, confidence."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .answers import Answer
from .normalize import (
    DEFAULT_TOL,
    Result,
    canonical_signature,
    cells_equal,
    compare_results,
    norm_cell,
)
from .questions import Question
from .sources import SourceError, SqlSource

# error classes
E_NONE = "none"
E_SYNTAX = "syntax"
E_COLUMN = "column_name"
E_TIMEOUT = "timeout"
E_OTHER = "other_error"
E_EMPTY = "empty_result"
E_WRONG = "wrong_result"
ERROR_CLASSES = (E_NONE, E_SYNTAX, E_COLUMN, E_TIMEOUT, E_OTHER, E_EMPTY, E_WRONG)

# interpretation classes
I_STATED = "stated"
I_ASKED = "asked"
I_SILENT = "silent"
I_NA = "not_applicable"

# confidence classes
C_CONFIDENT = "confident"
C_HEDGED = "hedged"
C_UNSTATED = "unstated"

_TIMEOUT_RE = re.compile(r"time(?:d)?[ -]?out|timeout|HYT0[01]|deadline|interrupted", re.I)
_COLUMN_RE = re.compile(
    r"no such (?:column|table)|invalid (?:column|object)|unknown (?:column|table)|ambiguous column|"
    r"column .* (?:not found|does not exist)|table .* (?:not found|does not exist)|"
    r"cannot resolve|unresolved_column|not_found|42S02|42S22", re.I)
_SYNTAX_RE = re.compile(
    r"syntax|incomplete input|parse error|parse_syntax|unrecognized token|near \"|unexpected token|42000", re.I)

ASSUMPTION_CUES = (
    "assum", "interpret", "treat", "defin", "i took", "taken to mean", "mean", "consider",
    "understood", "i used", "i've used", "based on", "by default", "taking", "i read",
)
_CONFIDENT_RE = re.compile(
    r"\b(definitely|certainly|certain that|clearly|obviously|without doubt|for sure|100 ?%|"
    r"guarantee[sd]?|confirmed|the (?:exact )?answer is|precisely|undoubtedly|no doubt)\b", re.I)
_HEDGE_RE = re.compile(
    r"\b(might|maybe|perhaps|possibly|probably|approximate(?:ly)?|roughly|not sure|unsure|"
    r"uncertain|likely|appears?|seems?|may be|could be|please verify|double[- ]check|"
    r"caveat|i think|i believe|low confidence|unable to confirm)\b", re.I)


@dataclass
class ScoreRow:
    id: str
    system: str
    run: int
    split: str = "dev"
    tags: list[str] = field(default_factory=list)
    graded: bool = True
    executed: bool = False
    match: Optional[bool] = None
    checks_pass: Optional[bool] = None
    correct: Optional[bool] = None
    error_class: str = E_NONE
    error: Optional[str] = None
    confidence: str = C_UNSTATED
    interpretation: str = I_NA
    seconds: Optional[float] = None
    confident_wrong: bool = False
    unhedged_wrong: bool = False
    signature: str = ""
    detail: str = ""
    rows_returned: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ScoreRow":
        names = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in names})


# ---------------------------------------------------------------------- pieces

def classify_error(msg: Optional[str]) -> str:
    if not msg:
        return E_OTHER
    if _TIMEOUT_RE.search(msg):
        return E_TIMEOUT
    if _COLUMN_RE.search(msg):
        return E_COLUMN
    if _SYNTAX_RE.search(msg):
        return E_SYNTAX
    return E_OTHER


def classify_confidence(*texts: str) -> str:
    blob = " ".join(t for t in texts if t)
    if _HEDGE_RE.search(blob):
        return C_HEDGED
    if _CONFIDENT_RE.search(blob):
        return C_CONFIDENT
    return C_UNSTATED


def classify_interpretation(q: Question, ans: Answer) -> str:
    """Three-way disclosure for questions that carry ``ambiguous_terms``:
    asked (the system asked a clarifying question), stated (every ambiguous term is mentioned
    together with an assumption cue), or silent (proceeded without saying how it read the term)."""
    if not q.ambiguous_terms:
        return I_NA
    if ans.clarified:
        return I_ASKED
    text = " ".join([ans.interpretation or "", ans.text or ""]).casefold()
    if not text.strip():
        return I_SILENT
    cues = tuple(c.casefold() for c in ASSUMPTION_CUES + tuple(q.assumption_cues))
    if not any(c in text for c in cues):
        return I_SILENT
    if all(t.casefold() in text for t in q.ambiguous_terms):
        return I_STATED
    return I_SILENT


def evaluate_checks(checks: dict, res: Result) -> tuple[bool, list[str]]:
    """Evaluate ``result_checks`` against a result. Returns (passed, failures)."""
    fails: list[str] = []
    rc = checks.get("row_count")
    if isinstance(rc, dict):
        n = len(res.rows)
        if rc.get("min") is not None and n < rc["min"]:
            fails.append("row_count %d < min %s" % (n, rc["min"]))
        if rc.get("max") is not None and n > rc["max"]:
            fails.append("row_count %d > max %s" % (n, rc["max"]))
    low = {str(c).strip().casefold(): i for i, c in enumerate(res.columns)}
    for col in checks.get("columns") or []:
        if str(col).strip().casefold() not in low:
            fails.append("column %r missing" % col)
    for v in checks.get("values") or []:
        idx = low.get(str(v.get("col", "")).strip().casefold())
        row_i = int(v.get("row", 0))
        if idx is None:
            fails.append("value check: column %r missing" % v.get("col"))
        elif row_i >= len(res.rows):
            fails.append("value check: no row %d" % row_i)
        else:
            tol = float(v.get("tol", DEFAULT_TOL))
            if not cells_equal(norm_cell(res.rows[row_i][idx]), norm_cell(v.get("equals")), tol):
                fails.append("value check: %s row %d is %r, expected %r" % (
                    v.get("col"), row_i, res.rows[row_i][idx], v.get("equals")))
    for want in checks.get("must_include_rows") or []:
        ok = False
        if isinstance(want, dict):
            for r in res.rows:
                good = True
                for col, val in want.items():
                    idx = low.get(str(col).strip().casefold())
                    if idx is None or not cells_equal(norm_cell(r[idx]), norm_cell(val), DEFAULT_TOL):
                        good = False
                        break
                if good:
                    ok = True
                    break
        if not ok:
            fails.append("required row not found: %r" % (want,))
    return (not fails), fails


def obtain_result(ans: Answer, source: Optional[SqlSource], timeout: float) -> tuple[Optional[Result], Optional[str]]:
    """The answer's own result if given, otherwise execute its SQL on the source."""
    if ans.error:
        return None, ans.error
    if ans.result is not None:
        return ans.result, None
    if ans.sql:
        if source is None:
            return None, "no result supplied and no SQL source configured to execute the SQL"
        try:
            return source.execute(ans.sql, timeout=timeout), None
        except SourceError as e:
            return None, e.message if e.kind != "timeout" else "timeout: " + e.message
    return None, "answer has neither a result nor SQL"


def score_answer(
    q: Question,
    baseline: Optional[Result],
    ans: Answer,
    source: Optional[SqlSource] = None,
    timeout: float = 30.0,
) -> ScoreRow:
    row = ScoreRow(id=q.id, system=ans.system, run=ans.run, split=q.split, tags=list(q.tags),
                   seconds=ans.seconds)
    checks = q.result_checks
    row.graded = baseline is not None or bool(checks)
    row.interpretation = classify_interpretation(q, ans)
    row.confidence = classify_confidence(ans.interpretation, ans.text)

    res, err = obtain_result(ans, source, timeout)
    if res is None:
        row.executed = False
        row.error = err
        row.error_class = classify_error(err)
        row.correct = False if row.graded else None
        row.signature = "ERR:" + row.error_class
        row.detail = "not executed"
        return row

    row.executed = True
    row.rows_returned = len(res.rows)
    row.signature = canonical_signature(res)
    cfg = q.compare_config()
    reasons: list[str] = []
    if baseline is not None:
        row.match, why = compare_results(baseline, res, cfg)
        if not row.match:
            reasons.append(why)
    if checks:
        row.checks_pass, fails = evaluate_checks(checks, res)
        reasons.extend(fails)
    if row.graded:
        row.correct = (row.match is not False) and (row.checks_pass is not False)
    row.detail = "; ".join(reasons)
    if row.correct is False:
        expects_rows = (baseline is not None and len(baseline.rows) > 0) or \
            ((checks.get("row_count") or {}).get("min") or 0) > 0
        row.error_class = E_EMPTY if (not res.rows and expects_rows) else E_WRONG
        row.confident_wrong = row.confidence == C_CONFIDENT
        row.unhedged_wrong = row.confidence != C_HEDGED
    return row
