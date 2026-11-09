"""Read-only claim and data header preflight, isolated from the harness interpreter.

The validity judge harness only ever learns engine CODES (INPUT_SCHEMA_ERROR,
INPUT_UNKNOWN_KEY, INPUT_MISSING_KEY); it never surfaces the engine's own messages, so a
worker is corrected four times without being told what is actually wrong. This module runs the
installed engine's own claim loader and compares the claim's column names against the real
header of the data_location file, and returns the engine's exact messages (unknown key paths,
the load_claim messages, and the missing columns together with the columns that do exist).

It also runs a DESIGN preflight that never reads the response column: it counts how many lots
each exposure group has in each shared stratum (product), using build_stratum_counts, and adds
a problem when the claim's own design cannot support a within-stratum contrast -- no stratifier
column is present at all, or no stratum is shared by both groups with enough lots. This only
ever ADDS problems (tightens the gate); it never clears one, and it opens only the stratifier
and unit columns, never the response.

It is deliberately a standalone script: judge_gate runs it in a separate interpreter with the
engine root on sys.path so importing the frozen engine never perturbs the harness process. It
reads only; it never writes to or edits anything under the engine root or the data file.
"""
import csv
import json
import os
import sys


def check(claim, data_location, engine_claim):
    """Return the engine's own problem messages for ``claim`` against ``data_location``.

    ``engine_claim`` is the frozen engine's ``validity.claim`` module. An empty list means the
    claim loads, every column it names exists in the data file, and the design can support a
    within-stratum contrast.
    """
    problems = []
    unknown = engine_claim.unknown_key_paths(claim)
    if unknown:
        problems.append("unknown keys: %s" % unknown)
    try:
        engine_claim.load_claim(claim)
    except Exception as exc:
        messages = getattr(exc, "msgs", None) or getattr(exc, "messages", None) or [str(exc)]
        problems.extend(str(message) for message in messages)
    columns = _header(data_location)
    if columns is not None:
        for section, key in (("response", "column"), ("response", "trials_column"),
                             ("exposure", "column"), ("hierarchy", "unit_column")):
            obj = claim.get(section)
            col = obj.get(key) if isinstance(obj, dict) else None
            if col and col not in columns:
                problems.append("%s.%s = %r is not a column of the data file; columns are %s"
                                % (section, key, col, columns))
    problems.extend(_design_problems(claim, data_location))
    return problems


def _design_problems(claim, data_location):
    """Design-preflight messages, or [] when the design axis cannot be evaluated.

    Counts lots per exposure group per shared stratum without ever reading the response column,
    and reports the two ways the design is empty before any analysis runs: no stratifier column
    exists to hold the groups apart, or no stratum is shared by both groups with at least the
    minimum number of lots. A design that cannot be read at all (adapter unavailable, file
    unreadable) is NOT a worker-fixable problem, so this returns [] rather than a spurious
    correction -- the same rule the engine preflight uses for an unavailable engine.
    """
    try:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from relay.design_stratum_counts import (build_stratum_counts, shared_stratum_summary,
                                                  K_MIN_LOTS)
    except Exception:
        return []
    try:
        result = build_stratum_counts(claim, data_location)
    except Exception:
        return []
    if result.get("response_column_read"):
        # Defensive: the adapter must never read the response. If it ever reports that it did,
        # refuse to turn that reading into a verdict.
        return []
    strat_cols = result.get("stratifier_columns") or []
    if not strat_cols:
        return ["design: no stratifier column is present, so the two exposure groups cannot be "
                "compared within a shared stratum; add a stratifier (e.g. product) column"]
    problems = []
    for col in strat_cols:
        counts = result.get("counts", {}).get(col, {})
        summary = shared_stratum_summary(counts, k_min=K_MIN_LOTS)
        if summary.get("shared_strata_meeting_k_min", 0) < 1:
            problems.append(
                "design: stratifier %r has no stratum shared by both groups with at least %d "
                "lots each (shared strata: %d, meeting the minimum: %d), so there is no "
                "within-stratum contrast to judge"
                % (col, summary.get("k_min", K_MIN_LOTS), summary.get("shared_strata", 0),
                   summary.get("shared_strata_meeting_k_min", 0)))
    return problems


def _header(data_location):
    """Return the first row (column names) of the data file, or None if it cannot be read."""
    ext = os.path.splitext(data_location)[1].lower()
    if ext in (".csv", ".txt"):
        for encoding in ("utf-8-sig", "cp932"):
            try:
                with open(data_location, encoding=encoding, newline="") as f:
                    return next(csv.reader(f))
            except (OSError, UnicodeError, StopIteration, csv.Error):
                continue
    elif ext in (".xlsx", ".xlsm"):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(data_location, read_only=True, data_only=True)
            try:
                return [str(c.value)
                        for c in next(wb[wb.sheetnames[0]].iter_rows(min_row=1, max_row=1))]
            finally:
                wb.close()
        except (OSError, ValueError, ImportError, StopIteration):
            return None
    return None


if __name__ == "__main__":
    sys.path.insert(0, sys.argv[1])
    from validity import claim as engine_claim
    with open(sys.argv[2], encoding="utf-8-sig") as f:
        request = json.load(f)
    print(json.dumps(check(request["registration"]["claim"], sys.argv[3], engine_claim),
                     ensure_ascii=False))
