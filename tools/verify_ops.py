"""Operator C - verification-loop helpers.

The orchestrator must never trust an LLM's self-reported result. These thin,
composable verifiers re-derive ground truth with local tools and compare it
against a claim, so the orchestrator can branch on the result.

Every verifier returns a one-line string that starts with "PASS: " or
"FAIL: " (or "[<tool> error: ...]" on failure) plus the observed value, so a
caller can dispatch purely on the prefix.
"""
import json
import math
import re
from typing import Optional

from .code_exec import run_python
from .file_ops import _validate_path
from .security import require_unlocked


def _to_float(value) -> Optional[float]:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def verify_python(code: str, expected: str, timeout: int = 60) -> str:
    """Run Python code and check its stdout against an expected string.

    Re-runs the code locally (via the same tempfile+subprocess path as
    run_python), captures stdout, and reports PASS/FAIL according to whether the
    stripped stdout equals or contains the expected string. Use this to confirm
    a claimed program output rather than trusting it.

    Args:
        code: Python source code to execute.
        expected: The output the orchestrator claims/expects (compared stripped).
        timeout: Maximum execution time in seconds.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        raw = run_python(code, timeout=timeout)
        # run_python may itself return a lock/error string; surface it.
        if raw.startswith("[locked client") or raw.startswith("[run_python error"):
            return f"FAIL: code did not run: {raw}"
        if raw.startswith("[timeout"):
            return f"FAIL: {raw}"
        stdout = ""
        if "[stdout]" in raw:
            after = raw.split("[stdout]", 1)[1]
            stdout = after.split("[stderr]", 1)[0]
            stdout = stdout.split("[returncode:", 1)[0]
        actual = stdout.strip()
        want = (expected or "").strip()
        if actual == want:
            return f"PASS: stdout equals expected ({actual!r})"
        if want and want in actual:
            return f"PASS: stdout contains expected (expected={want!r}, actual={actual!r})"
        return f"FAIL: expected={want!r}, actual stdout={actual!r}"
    except Exception as e:
        return f"[verify_python error: {type(e).__name__}: {e}]"


def verify_numeric_close(
    claimed,
    actual,
    rel_tol: float = 1e-6,
    abs_tol: float = 1e-9,
) -> str:
    """Compare a claimed number against an actual number within tolerance.

    Both values may be numbers or strings parseable as float. Reports PASS/FAIL
    with the absolute delta. Useful for checking an LLM's claimed numeric result
    against a locally-computed ground truth.

    Args:
        claimed: The value claimed (number or numeric string).
        actual: The value computed locally (number or numeric string).
        rel_tol: Relative tolerance (see math.isclose).
        abs_tol: Absolute tolerance (see math.isclose).
    """
    try:
        c = _to_float(claimed)
        a = _to_float(actual)
        if c is None or a is None:
            return f"FAIL: not numeric (claimed={claimed!r}, actual={actual!r})"
        delta = abs(c - a)
        if math.isclose(c, a, rel_tol=rel_tol, abs_tol=abs_tol):
            return f"PASS: claimed={c!r} matches actual={a!r} (delta={delta:.6g})"
        return f"FAIL: claimed={c!r} != actual={a!r} (delta={delta:.6g})"
    except Exception as e:
        return f"[verify_numeric_close error: {type(e).__name__}: {e}]"


def verify_file_contains(path: str, needle: str, regex: bool = False) -> str:
    """Check whether a file contains a substring or matches a regex.

    Reads the file through the allowed-base validator and reports PASS/FAIL.

    Args:
        path: File path under the allowed base.
        needle: Substring (or regex pattern when regex=True) to look for.
        regex: Treat needle as a regular expression when true.
    """
    try:
        p = _validate_path(path)
        if not p.is_file():
            return f"FAIL: no such file: {p}"
        text = p.read_text(encoding="utf-8", errors="replace")
        if regex:
            m = re.search(needle, text)
            if m:
                return f"PASS: regex {needle!r} matched (first match: {m.group(0)!r})"
            return f"FAIL: regex {needle!r} did not match in {p}"
        if needle in text:
            idx = text.index(needle)
            return f"PASS: substring found at offset {idx} in {p}"
        return f"FAIL: substring {needle!r} not found in {p}"
    except Exception as e:
        return f"[verify_file_contains error: {type(e).__name__}: {e}]"


def verify_json_schema(path_or_text: str, required_keys: list) -> str:
    """Check that a JSON object contains a set of required top-level keys.

    Parses JSON either from a file path under the allowed base (if the argument
    resolves to an existing file) or from inline text, then reports which of
    required_keys are present and which are missing.

    Args:
        path_or_text: A JSON file path under the allowed base, or inline JSON text.
        required_keys: List of top-level key names that must be present.
    """
    try:
        if not isinstance(required_keys, list):
            return "[verify_json_schema error: required_keys must be a list]"
        data = None
        source = "inline text"
        try:
            p = _validate_path(path_or_text)
            if p.is_file():
                data = json.loads(p.read_text(encoding="utf-8"))
                source = f"file {p}"
        except (PermissionError, OSError, ValueError):
            data = None
        if data is None:
            data = json.loads(path_or_text)
        if not isinstance(data, dict):
            return f"FAIL: parsed JSON ({source}) is not an object (got {type(data).__name__})"
        present = [k for k in required_keys if k in data]
        missing = [k for k in required_keys if k not in data]
        if not missing:
            return f"PASS: all {len(required_keys)} required key(s) present ({source})"
        return f"FAIL: missing {missing} ; present {present} ({source})"
    except Exception as e:
        return f"[verify_json_schema error: {type(e).__name__}: {e}]"


def verify_table_stat(
    path: str,
    column: str,
    op: str,
    value,
    tol: float = 1e-6,
) -> str:
    """Compute a statistic on a CSV/Excel column and compare it to a value.

    Reads the table with pandas through the allowed-base validator, computes the
    requested aggregate over `column`, and reports PASS/FAIL with the computed
    number. Lets the orchestrator independently confirm a claimed aggregate.

    Args:
        path: CSV or Excel file path under the allowed base.
        column: Column name to aggregate.
        op: One of sum, mean, min, max, count, nunique.
        value: The claimed/expected value to compare against (numeric).
        tol: Absolute tolerance for the comparison.
    """
    try:
        import pandas as pd

        ops = {"sum", "mean", "min", "max", "count", "nunique"}
        if op not in ops:
            return f"[verify_table_stat error: op must be one of {sorted(ops)}]"
        p = _validate_path(path)
        if not p.is_file():
            return f"FAIL: no such file: {p}"
        suffix = p.suffix.lower()
        if suffix == ".csv":
            df = pd.read_csv(p, encoding="utf-8-sig")
        elif suffix in {".xlsx", ".xls"}:
            df = pd.read_excel(p, sheet_name=0)
        else:
            return "[verify_table_stat error: supported formats are .csv, .xlsx, .xls]"
        if column not in df.columns:
            return f"FAIL: column {column!r} not in table (columns: {list(df.columns)})"
        series = df[column]
        computed = getattr(series, op)()
        expected = _to_float(value)
        if expected is None:
            return f"FAIL: expected value {value!r} is not numeric (computed {op}={computed})"
        delta = abs(float(computed) - expected)
        if delta <= tol:
            return f"PASS: {op}({column})={computed} matches {expected} (delta={delta:.6g})"
        return f"FAIL: {op}({column})={computed} != {expected} (delta={delta:.6g})"
    except Exception as e:
        return f"[verify_table_stat error: {type(e).__name__}: {e}]"
