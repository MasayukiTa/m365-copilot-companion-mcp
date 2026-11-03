"""Read-only claim and data header preflight, isolated from the harness interpreter.

The validity judge harness only ever learns engine CODES (INPUT_SCHEMA_ERROR,
INPUT_UNKNOWN_KEY, INPUT_MISSING_KEY); it never surfaces the engine's own messages, so a
worker is corrected four times without being told what is actually wrong. This module runs the
installed engine's own claim loader and compares the claim's column names against the real
header of the data_location file, and returns the engine's exact messages (unknown key paths,
the load_claim messages, and the missing columns together with the columns that do exist).

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
    claim loads and every column it names exists in the data file.
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
