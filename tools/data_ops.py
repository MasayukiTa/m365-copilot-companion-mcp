import io
import json
from typing import Optional

import pandas as pd

from .file_ops import _validate_path
from .security import require_unlocked


def _read_table(path: str, sheet_name: Optional[str] = None) -> pd.DataFrame:
    p = _validate_path(path)
    suffix = p.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(p, encoding="utf-8-sig")
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(p, sheet_name=sheet_name or 0)
    raise ValueError("Supported table formats are .csv, .xlsx, and .xls")


def read_excel(path: str, sheet_name: Optional[str] = None, max_rows: int = 200) -> str:
    """Read an Excel or CSV file and return a text preview."""
    try:
        df = _read_table(path, sheet_name)
        total = len(df)
        preview = df.head(max_rows)
        suffix = f"\n(total rows: {total}, shown rows: {len(preview)})"
        return preview.to_string(index=False) + suffix
    except Exception as e:
        return f"[read_excel error: {type(e).__name__}: {e}]"


def write_excel(path: str, csv_data: str, sheet_name: str = "Sheet1") -> str:
    """Write CSV-formatted text into an Excel .xlsx file."""
    locked = require_unlocked()
    if locked:
        return locked
    try:
        p = _validate_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        df = pd.read_csv(io.StringIO(csv_data))
        df.to_excel(p, sheet_name=sheet_name, index=False)
        return f"Wrote Excel file {p} ({len(df)} rows)"
    except Exception as e:
        return f"[write_excel error: {type(e).__name__}: {e}]"


def summarize_table(path: str, sheet_name: Optional[str] = None) -> str:
    """Return columns, row count, null counts, and numeric summary for a CSV or Excel file."""
    try:
        df = _read_table(path, sheet_name)
        lines = [f"rows: {len(df)}", f"columns: {len(df.columns)}"]
        lines.append("column names: " + ", ".join(map(str, df.columns)))
        nulls = df.isna().sum()
        if nulls.any():
            lines.append("null counts:")
            lines.extend(f"- {col}: {count}" for col, count in nulls.items() if count)
        numeric = df.select_dtypes(include="number")
        if not numeric.empty:
            lines.append("numeric summary:")
            lines.append(numeric.describe().to_string())
        return "\n".join(lines)
    except Exception as e:
        return f"[summarize_table error: {type(e).__name__}: {e}]"


def read_json(path: str, indent: int = 2) -> str:
    """Read and pretty-print a JSON file."""
    try:
        p = _validate_path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        return json.dumps(data, ensure_ascii=False, indent=indent)
    except Exception as e:
        return f"[read_json error: {type(e).__name__}: {e}]"


def write_json(path: str, json_data: str, indent: int = 2) -> str:
    """Validate and write JSON text to a file."""
    locked = require_unlocked()
    if locked:
        return locked
    try:
        p = _validate_path(path)
        data = json.loads(json_data)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")
        return f"Wrote JSON file {p}"
    except Exception as e:
        return f"[write_json error: {type(e).__name__}: {e}]"
