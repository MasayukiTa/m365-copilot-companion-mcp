"""Generic data-to-chart tools: a PNG, a native editable Excel chart, a python-drawn image.

Three tools, all taking a plain data table as input (a CSV/xlsx path, or rows passed inline):

  chart_render        table -> PNG picture (matplotlib)
  excel_chart_export  table -> .xlsx with a data sheet, a NATIVE Excel chart that points at
                      the data cells (editable and refreshable in Excel), and an explanation sheet
  chart_from_python   matplotlib code -> PNG, run in a child process (GATED: arbitrary code)

All three write files, so all three are behind require_unlocked() and resolve paths with
file_ops._validate_path, exactly like odbc_to_excel.

THIRD-PARTY IMPORTS ARE LAZY AND NAMED. pandas, matplotlib, openpyxl and xlsxwriter are all in
requirements.txt (so setup/quickstart installs them on a fresh PC). They are imported inside the
tool bodies, never at module import, so a PC where one is missing still starts the server and the
tool answers with a plain "run setup/quickstart again" error instead of crashing.
tools/test_chart_ops_dependencies.py pins that every third-party import here is declared.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import re
import shutil
import sys
import uuid
import warnings
from pathlib import Path
from typing import Any, Optional, Union

from .file_ops import _validate_path
from .security import require_unlocked

#: Rows read from the input before the rest is dropped (and said so in the answer).
DEFAULT_MAX_ROWS = 10000
HARD_MAX_ROWS = 200000
#: Fonts tried in order for Japanese text. The first one installed wins.
JP_FONT_CANDIDATES = (
    "Meiryo", "Yu Gothic", "YuGothic", "MS Gothic", "MS PGothic",
    "Noto Sans CJK JP", "Noto Sans JP", "IPAexGothic", "IPAGothic", "TakaoGothic",
    "Hiragino Sans", "Hiragino Kaku Gothic ProN",
)

CHART_KINDS = ("bar", "line", "scatter", "hist", "pie", "stacked_bar", "box")
EXCEL_KINDS = ("column", "bar", "line", "scatter", "pie", "stacked_column", "stacked_bar", "area")
_EXCEL_ALIASES = {"stacked": "stacked_column", "col": "column", "columns": "column"}

_MISSING_DEP = "requirements.txt の依存が未導入です。setup/quickstart を再実行してください"


class _MissingDependency(Exception):
    pass


class _ToolError(Exception):
    """A user-facing problem; the message is returned inside '[<tool> error: ...]'."""


def _need(module: str):
    """Import a third-party module lazily; a missing one becomes a plain setup message."""
    try:
        import importlib
        return importlib.import_module(module)
    except ImportError as e:
        raise _MissingDependency(f"{_MISSING_DEP} (module: {module}; {e})") from e


def _fail(tool: str, e: Exception) -> str:
    if isinstance(e, _MissingDependency):
        return f"[{tool} error: {e}]"
    if isinstance(e, _ToolError):
        return f"[{tool} error: {e}]"
    return f"[{tool} error: {type(e).__name__}: {e}]"


# --------------------------------------------------------------------------- input


def _as_list(v: Any) -> list[str]:
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        return [str(i).strip() for i in v if str(i).strip()]
    return [s.strip() for s in str(v).split(",") if s.strip()]


def _clamp_rows(max_rows: int) -> int:
    try:
        n = int(max_rows)
    except (TypeError, ValueError):
        n = DEFAULT_MAX_ROWS
    return max(1, min(n, HARD_MAX_ROWS))


def _read_csv(pd, path: Path, limit: int):
    last: Optional[Exception] = None
    for enc in ("utf-8-sig", "cp932"):
        try:
            df = pd.read_csv(path, encoding=enc, nrows=limit + 1)
            return df
        except UnicodeDecodeError as e:
            last = e
    raise _ToolError(f"cannot decode CSV as UTF-8 or Shift-JIS: {last}")


def _read_xlsx_range(path: Path, sheet: str, cell_range: str, limit: int):
    openpyxl = _need("openpyxl")
    pd = _need("pandas")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet] if sheet else wb.worksheets[0]
        if cell_range:
            block = ws[cell_range]
            if not isinstance(block, tuple) or (block and not isinstance(block[0], tuple)):
                block = (block,) if not isinstance(block, tuple) else (block,)
            rows_iter = ([c.value for c in row] for row in block)
        else:
            rows_iter = (list(r) for r in ws.iter_rows(values_only=True))
        out = []
        for i, r in enumerate(rows_iter):
            out.append(r)
            if i >= limit + 1:
                break
    finally:
        wb.close()
    if not out:
        raise _ToolError("the sheet/range is empty")
    header = [("" if h is None else str(h)) or f"col{j + 1}" for j, h in enumerate(out[0])]
    return pd.DataFrame(out[1:], columns=header)


def _load_table(data_path: str, rows: Any, sheet: str, cell_range: str, max_rows: int):
    """Return (DataFrame, source_text, truncated_note). Rows beyond max_rows are dropped."""
    pd = _need("pandas")
    limit = _clamp_rows(max_rows)
    if rows not in (None, "", []):
        if isinstance(rows, str):
            try:
                rows = json.loads(rows)
            except ValueError as e:
                raise _ToolError(f"rows is not valid JSON: {e}")
        if not isinstance(rows, list) or not rows:
            raise _ToolError("rows must be a non-empty list of objects (or of equal-length lists)")
        if isinstance(rows[0], dict):
            df = pd.DataFrame(rows)
        else:
            df = pd.DataFrame(rows[1:], columns=[str(c) for c in rows[0]])
        source = "inline rows"
    elif data_path:
        p = _validate_path(data_path)
        if not p.is_file():
            raise _ToolError(f"data_path not found: {p}")
        suffix = p.suffix.lower()
        if suffix in (".csv", ".txt", ".tsv"):
            df = _read_csv(pd, p, limit) if suffix != ".tsv" else pd.read_csv(
                p, sep="\t", encoding="utf-8-sig", nrows=limit + 1)
            source = str(p)
        elif suffix in (".xlsx", ".xlsm"):
            df = _read_xlsx_range(p, sheet, cell_range, limit)
            source = f"{p} [sheet={sheet or '(first)'}, range={cell_range or '(all)'}]"
        else:
            raise _ToolError("data_path must be .csv, .tsv or .xlsx")
    else:
        raise _ToolError("give data_path (CSV/xlsx) or rows")
    if df.empty or len(df.columns) == 0:
        raise _ToolError("the input table has no data rows")
    note = ""
    if len(df) > limit:
        note = f"input truncated to the first {limit} rows (max_rows={limit})"
        df = df.iloc[:limit].copy()
    df.columns = [str(c) for c in df.columns]
    return df, source, note


def _pick_columns(df, x: str, ys: list[str], group: str, tool: str):
    cols = list(df.columns)
    for name in ([x] if x else []) + ys + ([group] if group else []):
        if name not in cols:
            raise _ToolError(f"column {name!r} not found. Columns: {', '.join(cols)}")


def _numeric(pd, s):
    return pd.to_numeric(s, errors="coerce")


def _default_ys(pd, df, x: str, group: str) -> list[str]:
    for c in df.columns:
        if c in (x, group):
            continue
        if _numeric(pd, df[c]).notna().sum() >= max(1, len(df) // 2):
            return [c]
    raise _ToolError("no numeric column to plot; pass y")


# --------------------------------------------------------------------------- fonts


def _find_font(font_manager) -> Optional[str]:
    have = {f.name for f in font_manager.fontManager.ttflist}
    for cand in JP_FONT_CANDIDATES:
        if cand in have:
            return cand
    return None


def _font_context(matplotlib):
    """(rc dict, warning). Japanese font if one is installed, else the default plus a warning."""
    from matplotlib import font_manager
    rc = {"axes.unicode_minus": False}
    font = _find_font(font_manager)
    if font:
        rc["font.family"] = [font]
        return rc, ""
    return rc, ("no Japanese font found (looked for: " + ", ".join(JP_FONT_CANDIDATES[:4])
                + ", ...); Japanese text may render as boxes")


# --------------------------------------------------------------------------- paths


def _unique_path(p: Path) -> Path:
    """p, or p with _2, _3 ... before the suffix when p already exists."""
    if not p.exists():
        return p
    n = 2
    while True:
        cand = p.with_name(f"{p.stem}_{n}{p.suffix}")
        if not cand.exists():
            return cand
        n += 1


def _default_out(prefix: str, suffix: str) -> str:
    try:
        from relay.temp_home import temp_dir
        d = temp_dir("charts")
    except Exception:
        import tempfile
        d = os.path.join(tempfile.gettempdir(), "m365-companion", "charts")
        os.makedirs(d, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(d, f"{prefix}_{stamp}{suffix}")


def _out_path(out_path: str, prefix: str, suffix: str) -> Path:
    """The caller's out_path (validated against the allowed base) or a generated file in the
    swept temp home, which is ours to write and needs no base check."""
    if out_path:
        return _validate_path(out_path)
    return Path(_default_out(prefix, suffix))


def _parse_size(size: str) -> tuple[float, float]:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[x,*]\s*(\d+(?:\.\d+)?)\s*", str(size or ""))
    if not m:
        return 10.0, 6.0
    w, h = float(m.group(1)), float(m.group(2))
    return max(2.0, min(w, 30.0)), max(2.0, min(h, 30.0))


# --------------------------------------------------------------------------- chart_render


def _prepare_categories(pd, df, x: str, ys: list[str], group: str, top_n: int, sort: str):
    """Aggregate (sum) y per x[, group], then sort and keep the top N categories.

    Returns (wide DataFrame indexed by x with one column per series, series names, notes).
    """
    notes = []
    work = df.copy()
    for y in ys:
        work[y] = _numeric(pd, work[y])
    if group:
        base = work.pivot_table(index=x, columns=group, values=ys[0], aggfunc="sum", fill_value=0)
        base.columns = [str(c) for c in base.columns]
    else:
        base = work.groupby(x, sort=False)[ys].sum()
    total = base.sum(axis=1)
    order = sort.lower() if sort else ""
    if top_n and top_n > 0:
        if order not in ("asc", "desc"):
            order = "desc"
        keep = total.sort_values(ascending=False).head(top_n).index
        if len(keep) < len(base):
            notes.append(f"top_n={top_n}: kept {len(keep)} of {len(base)} categories")
        base = base.loc[keep]
        total = total.loc[keep]
    if order in ("asc", "desc"):
        base = base.loc[total.sort_values(ascending=(order == "asc")).index]
    return base, list(base.columns), notes


def chart_render(
    kind: str,
    data_path: str = "",
    rows: Optional[Any] = None,
    x: str = "",
    y: Union[str, list[str], None] = None,
    group: str = "",
    title: str = "",
    xlabel: str = "",
    ylabel: str = "",
    top_n: int = 0,
    sort: str = "",
    out_path: str = "",
    dpi: int = 150,
    size: str = "10x6",
    sheet: str = "",
    cell_range: str = "",
    max_rows: int = DEFAULT_MAX_ROWS,
) -> str:
    """Draw a chart from a data table and save it as a PNG picture.

    Input is a CSV/xlsx file (data_path, with optional sheet and cell_range such as
    "A1:D50", first row = header) OR rows passed inline (a list of objects, or a list of
    lists whose first item is the header). Nothing about the data is assumed: any columns.

    GATED: writes a file, so it needs the same per-IP unlock as a write. Call
    unlock(password=...) first if refused.

    Args:
        kind: bar | line | scatter | hist | pie | stacked_bar | box.
        data_path: CSV (.csv/.tsv) or Excel (.xlsx) path under the allowed base.
        rows: Inline data instead of data_path.
        x: Category / horizontal column (bar, line, scatter, pie labels, box groups).
        y: Value column, or several (list, or comma-separated). Default: first numeric column.
        group: Optional column that splits the data into coloured series.
        title: Chart title (Japanese is fine).
        xlabel: Horizontal axis label.
        ylabel: Vertical axis label.
        top_n: Keep only the N largest categories (bar, stacked_bar, pie, box) or rows.
        sort: asc | desc, by the value (bar, stacked_bar, pie).
        out_path: Output .png path. Default: a file under the temp home (%TEMP%\\m365-companion\\charts).
        dpi: Image resolution (50-400).
        size: "WIDTHxHEIGHT" in inches, e.g. "10x6".
        sheet: Sheet name for xlsx input (default first sheet).
        cell_range: Cell range for xlsx input, e.g. "A1:D200".
        max_rows: Input rows read at most (default 10000, ceiling 200000); the answer says
            when rows were dropped.

    Returns: the PNG path and a text summary (points, axes, series, notes), or
    "[chart_render error: ...]".
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        return _chart_render(kind, data_path, rows, x, y, group, title, xlabel, ylabel,
                             top_n, sort, out_path, dpi, size, sheet, cell_range, max_rows)
    except Exception as e:
        return _fail("chart_render", e)


def _chart_render(kind, data_path, rows, x, y, group, title, xlabel, ylabel,
                  top_n, sort, out_path, dpi, size, sheet, cell_range, max_rows) -> str:
    kind = (kind or "").strip().lower()
    if kind not in CHART_KINDS:
        raise _ToolError(f"kind must be one of: {', '.join(CHART_KINDS)}")
    pd = _need("pandas")
    matplotlib = _need("matplotlib")
    from matplotlib.figure import Figure

    out = _out_path(out_path, "chart", ".png")
    if out.suffix.lower() != ".png":
        raise _ToolError("out_path must end with .png")

    df, source, trunc = _load_table(data_path, rows, sheet, cell_range, max_rows)
    ys = _as_list(y)
    x = (x or "").strip()
    group = (group or "").strip()
    if not ys:
        ys = _default_ys(pd, df, x, group)
    _pick_columns(df, x, ys, group, "chart_render")
    if kind in ("bar", "line", "scatter", "pie", "stacked_bar") and not x:
        x = next((c for c in df.columns if c not in ys and c != group), df.columns[0])
    top_n = int(top_n or 0)
    notes = [trunc] if trunc else []
    rc, font_warn = _font_context(matplotlib)
    if font_warn:
        notes.append("WARNING: " + font_warn)
    try:
        dpi = max(50, min(int(dpi), 400))
    except (TypeError, ValueError):
        dpi = 150
    w, h = _parse_size(size)

    with matplotlib.rc_context(rc):
        fig = Figure(figsize=(w, h))
        ax = fig.add_subplot(111)
        points = 0
        series: list[str] = []

        if kind in ("bar", "stacked_bar"):
            base, series, n2 = _prepare_categories(pd, df, x, ys, group, top_n, sort)
            notes += n2
            labels = [str(i) for i in base.index]
            base.plot(kind="bar", stacked=(kind == "stacked_bar"), ax=ax, legend=len(series) > 1,
                      rot=45 if len(labels) > 6 else 0)
            ax.set_xticklabels(labels, ha="right" if len(labels) > 6 else "center")
            points = int(base.size)
        elif kind == "pie":
            base, series, n2 = _prepare_categories(pd, df, x, ys[:1], group, top_n, sort)
            notes += n2
            vals = base.sum(axis=1)
            vals = vals[vals > 0]
            ax.pie(vals.values, labels=[str(i) for i in vals.index], autopct="%1.1f%%")
            ax.axis("equal")
            series = [ys[0]]
            points = int(len(vals))
        elif kind == "line":
            work = df.copy()
            for c in ys:
                work[c] = _numeric(pd, work[c])
            xs_num = _numeric(pd, work[x])
            ordered = False
            if xs_num.notna().all():
                work[x] = xs_num
                ordered = True
            else:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    dt = pd.to_datetime(work[x], errors="coerce")
                if dt.notna().all():
                    work[x] = dt
                    ordered = True
                else:
                    work[x] = work[x].astype(str)   # categories keep their input order
            if ordered:
                work = work.sort_values(x)
            if top_n:
                work = work.sort_values(ys[0], ascending=False).head(top_n)
                if ordered:
                    work = work.sort_values(x)
                notes.append(f"top_n={top_n}: kept the {len(work)} largest rows")
            if group:
                for gname, part in work.groupby(group, sort=False):
                    ax.plot(part[x], part[ys[0]], marker="o", markersize=3, label=str(gname))
                    series.append(str(gname))
                    points += len(part)
            else:
                for c in ys:
                    ax.plot(work[x], work[c], marker="o", markersize=3, label=c)
                    series.append(c)
                    points += int(work[c].notna().sum())
            if len(series) > 1:
                ax.legend()
            fig.autofmt_xdate()
        elif kind == "scatter":
            work = df.copy()
            work[x] = _numeric(pd, work[x])
            work[ys[0]] = _numeric(pd, work[ys[0]])
            work = work.dropna(subset=[x, ys[0]])
            if work.empty:
                raise _ToolError("scatter needs numeric x and y columns")
            if top_n:
                work = work.sort_values(ys[0], ascending=False).head(top_n)
                notes.append(f"top_n={top_n}: kept the {len(work)} largest rows")
            if group:
                for gname, part in work.groupby(group, sort=False):
                    ax.scatter(part[x], part[ys[0]], label=str(gname), s=18, alpha=0.8)
                    series.append(str(gname))
                ax.legend()
            else:
                ax.scatter(work[x], work[ys[0]], s=18, alpha=0.8)
                series = [ys[0]]
            points = int(len(work))
        elif kind == "hist":
            col = ys[0]
            work = df.copy()
            work[col] = _numeric(pd, work[col])
            work = work.dropna(subset=[col])
            if work.empty:
                raise _ToolError("hist needs a numeric y column")
            if group:
                for gname, part in work.groupby(group, sort=False):
                    ax.hist(part[col], bins=20, alpha=0.6, label=str(gname))
                    series.append(str(gname))
                ax.legend()
            else:
                ax.hist(work[col], bins=20)
                series = [col]
            points = int(len(work))
            ax.set_ylabel("count")
        elif kind == "box":
            work = df.copy()
            for c in ys:
                work[c] = _numeric(pd, work[c])
            cat = x or group
            if cat:
                cats = list(work.groupby(cat, sort=False)[ys[0]].count().index)
                if top_n:
                    sums = work.groupby(cat)[ys[0]].sum().sort_values(ascending=False)
                    cats = [c for c in sums.head(top_n).index]
                    notes.append(f"top_n={top_n}: kept {len(cats)} categories")
                data = [work.loc[work[cat] == c, ys[0]].dropna().values for c in cats]
                labels = [str(c) for c in cats]
                series = [ys[0]]
            else:
                data = [work[c].dropna().values for c in ys]
                labels = list(ys)
                series = list(ys)
            ax.boxplot(data, tick_labels=labels) if "tick_labels" in _boxplot_params(ax) \
                else ax.boxplot(data, labels=labels)
            points = int(sum(len(d) for d in data))

        if title:
            ax.set_title(title)
        if kind not in ("pie", "hist"):
            ax.set_xlabel(xlabel or x or "")
            ax.set_ylabel(ylabel or (ys[0] if len(ys) == 1 else ""))
        elif kind == "hist":
            ax.set_xlabel(xlabel or ys[0])
            if ylabel:
                ax.set_ylabel(ylabel)
        fig.tight_layout()
        out.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out, dpi=dpi, format="png", facecolor="white")

    size_bytes = out.stat().st_size if out.exists() else 0
    if size_bytes <= 0:
        raise _ToolError("the PNG was not written")
    lines = [
        f"Wrote {out} ({size_bytes} bytes, {int(w * dpi)}x{int(h * dpi)} px)",
        f"kind={kind}  points={points}  x={x or '-'}  y={', '.join(ys)}  group={group or '-'}",
        f"series ({len(series)}): {', '.join(series[:12])}{' ...' if len(series) > 12 else ''}",
        f"source: {source}  (input rows used: {len(df)})",
    ]
    lines += [f"note: {n}" for n in notes]
    return "\n".join(lines)


def _boxplot_params(ax) -> set[str]:
    import inspect
    return set(inspect.signature(ax.boxplot).parameters)


# --------------------------------------------------------------------------- excel_chart_export


def _display_width(s: str) -> int:
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)


def _excel_cell(ws, r: int, c: int, v: Any, fmt_date):
    pd = _need("pandas")
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return
    try:
        if pd.isna(v):
            return
    except (TypeError, ValueError):
        pass
    if isinstance(v, bool):
        ws.write_boolean(r, c, v)
    elif isinstance(v, (int, float)) or type(v).__module__ == "numpy":
        try:
            f = float(v)
        except (TypeError, ValueError):
            ws.write_string(r, c, str(v))
            return
        if math.isinf(f):
            ws.write_string(r, c, str(v))
        else:
            ws.write_number(r, c, f)
    elif isinstance(v, (pd.Timestamp, _dt.datetime)):
        ws.write_datetime(r, c, pd.Timestamp(v).to_pydatetime().replace(tzinfo=None), fmt_date)
    elif isinstance(v, _dt.date):
        ws.write_datetime(r, c, _dt.datetime(v.year, v.month, v.day), fmt_date)
    else:
        ws.write_string(r, c, str(v))


def _write_table(wb, ws, df, header_fmt, date_fmt):
    for j, name in enumerate(df.columns):
        ws.write_string(0, j, str(name), header_fmt)
    widths = [_display_width(str(c)) for c in df.columns]
    for i, row in enumerate(df.itertuples(index=False, name=None), start=1):
        for j, v in enumerate(row):
            _excel_cell(ws, i, j, v, date_fmt)
            if i <= 200:
                widths[j] = max(widths[j], _display_width("" if v is None else str(v)))
    for j, wd in enumerate(widths):
        ws.set_column(j, j, min(max(wd + 2, 8), 50))
    ws.autofilter(0, 0, max(len(df), 1), len(df.columns) - 1)
    ws.freeze_panes(1, 0)


def excel_chart_export(
    kind: str,
    data_path: str = "",
    rows: Optional[Any] = None,
    x: str = "",
    y: Union[str, list[str], None] = None,
    group: str = "",
    title: str = "",
    xlabel: str = "",
    ylabel: str = "",
    top_n: int = 0,
    sort: str = "",
    out_path: str = "",
    notes: str = "",
    request: str = "",
    source: str = "",
    overwrite: bool = False,
    sheet: str = "",
    cell_range: str = "",
    max_rows: int = DEFAULT_MAX_ROWS,
) -> str:
    """Make an editable Excel workbook: data sheet + NATIVE Excel chart + explanation sheet.

    The chart is a real Excel chart object whose series point at cells of the data sheet, so
    it can be edited, restyled and refreshed in Excel (it is not a pasted picture). Sheets:
    "データ" (the table, header row, auto-filter, fitted column widths), "グラフ" (the chart),
    "説明" (request, data source, aggregation conditions, created time, row count, notes).
    When group / top_n / sort is used, the chart points at a small "集計" sheet holding the
    summed values instead (the "説明" sheet says so).

    GATED: writes a file, so it needs the same per-IP unlock as a write. Call
    unlock(password=...) first if refused. An existing file is NEVER overwritten unless
    overwrite=True: otherwise the name gets _2, _3 ... and the answer states the real path.

    Args:
        kind: column | bar | line | scatter | pie | stacked_column | stacked_bar | area
            ("stacked" = stacked_column; column = vertical bars, bar = horizontal bars).
        data_path: CSV (.csv/.tsv) or Excel (.xlsx) input path, under the allowed base.
        rows: Inline data instead of data_path (list of objects, or lists with a header first).
        x: Category / horizontal column. Default: the first non-value column.
        y: Value column, or several (list or comma-separated). Default: first numeric column.
        group: Optional column that splits the values into one series per value.
        title: Chart title.
        xlabel: Horizontal axis title.
        ylabel: Vertical axis title.
        top_n: Keep only the N largest categories (goes through the "集計" sheet).
        sort: asc | desc by value (goes through the "集計" sheet).
        out_path: Output .xlsx path. Default: a file under the temp home.
        notes: Free text written to the "説明" sheet.
        request: The original request sentence, written to the "説明" sheet.
        source: Where the data came from, for the "説明" sheet (default: the input path).
        overwrite: True to replace an existing file (default False).
        sheet: Sheet name for xlsx input.
        cell_range: Cell range for xlsx input, e.g. "A1:D200".
        max_rows: Input rows read at most (default 10000, ceiling 200000).

    Returns: the xlsx path and a summary, or "[excel_chart_export error: ...]".
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        return _excel_chart_export(kind, data_path, rows, x, y, group, title, xlabel, ylabel,
                                   top_n, sort, out_path, notes, request, source, overwrite,
                                   sheet, cell_range, max_rows)
    except Exception as e:
        return _fail("excel_chart_export", e)


def _excel_chart_export(kind, data_path, rows, x, y, group, title, xlabel, ylabel,
                        top_n, sort, out_path, notes, request, source, overwrite,
                        sheet, cell_range, max_rows) -> str:
    kind = _EXCEL_ALIASES.get((kind or "").strip().lower(), (kind or "").strip().lower())
    if kind not in EXCEL_KINDS:
        raise _ToolError(f"kind must be one of: {', '.join(EXCEL_KINDS)} (stacked = stacked_column)")
    pd = _need("pandas")
    xlsxwriter = _need("xlsxwriter")

    target = _out_path(out_path, "chart", ".xlsx")
    if target.suffix.lower() != ".xlsx":
        raise _ToolError("out_path must end with .xlsx")
    if not overwrite:
        target = _unique_path(target)

    df, src, trunc = _load_table(data_path, rows, sheet, cell_range, max_rows)
    ys = _as_list(y)
    x = (x or "").strip()
    group = (group or "").strip()
    if not ys:
        ys = _default_ys(pd, df, x, group)
    _pick_columns(df, x, ys, group, "excel_chart_export")
    if not x:
        x = next((c for c in df.columns if c not in ys and c != group), df.columns[0])
    top_n = int(top_n or 0)
    order = (sort or "").lower()
    if order not in ("", "asc", "desc"):
        raise _ToolError("sort must be asc or desc")

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".{uuid.uuid4().hex[:8]}.tmp")
    wb = xlsxwriter.Workbook(str(tmp), {"nan_inf_to_errors": True})
    try:
        header_fmt = wb.add_format({"bold": True, "bg_color": "#D9E1F2", "border": 1})
        date_fmt = wb.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
        ws_data = wb.add_worksheet("データ")
        ws_chart = wb.add_worksheet("グラフ")
        ws_note = wb.add_worksheet("説明")
        _write_table(wb, ws_data, df, header_fmt, date_fmt)

        # Where the chart reads from: the data sheet itself unless it needs aggregation.
        ref_sheet, ref_df = "データ", df
        cat_col = list(df.columns).index(x)
        val_cols = [list(df.columns).index(c) for c in ys]
        summary_sheet = False
        n_ref = len(df)
        if group or top_n or order:
            ws_sum = wb.add_worksheet("集計")
            work = df.copy()
            for c in ys:
                work[c] = pd.to_numeric(work[c], errors="coerce")
            if group:
                base = work.pivot_table(index=x, columns=group, values=ys[0],
                                        aggfunc="sum", fill_value=0, sort=False)
                base.columns = [str(c) for c in base.columns]
            else:
                base = work.groupby(x, sort=False)[ys].sum()
            total = base.sum(axis=1)
            if top_n:
                base = base.loc[total.sort_values(ascending=False).head(top_n).index]
                total = total.loc[base.index]
            if order:
                base = base.loc[total.sort_values(ascending=(order == "asc")).index]
            agg = base.reset_index()
            agg.columns = [str(c) for c in agg.columns]
            _write_table(wb, ws_sum, agg, header_fmt, date_fmt)
            ref_sheet, ref_df = "集計", agg
            cat_col = 0
            val_cols = list(range(1, len(agg.columns)))
            n_ref = len(agg)
            summary_sheet = True

        ctype, subtype = {
            "column": ("column", None), "bar": ("bar", None), "line": ("line", None),
            "scatter": ("scatter", None), "pie": ("pie", None), "area": ("area", None),
            "stacked_column": ("column", "stacked"), "stacked_bar": ("bar", "stacked"),
        }[kind]
        spec = {"type": ctype}
        if subtype:
            spec["subtype"] = subtype
        chart = wb.add_chart(spec)
        last = n_ref  # header is row 0; data rows 1..n_ref
        use_cols = val_cols[:1] if kind == "pie" else val_cols
        for vc in use_cols:
            chart.add_series({
                "name": [ref_sheet, 0, vc],
                "categories": [ref_sheet, 1, cat_col, last, cat_col],
                "values": [ref_sheet, 1, vc, last, vc],
            })
        if title:
            chart.set_title({"name": title})
        if kind != "pie":
            chart.set_x_axis({"name": xlabel or x})
            chart.set_y_axis({"name": ylabel or (ys[0] if len(ys) == 1 else "")})
        chart.set_size({"width": 900, "height": 520})
        ws_chart.insert_chart("B2", chart)

        created = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cond = [f"グラフ種類: {kind}", f"X列: {x}", f"Y列: {', '.join(ys)}"]
        if group:
            cond.append(f"色分け列: {group}（値は合計して「集計」シートに出力）")
        if top_n:
            cond.append(f"上位{top_n}件のみ（Y列の合計順）")
        if order:
            cond.append(f"並び順: {order}")
        cond.append("グラフの参照先: " + ("「集計」シート" if summary_sheet else "「データ」シート"))
        info = [
            ("依頼文", request),
            ("データの出所", source or src),
            ("集計条件", "\n".join(cond)),
            ("作成日時", created),
            ("件数", f"{len(df)} 行（データシート）" + (f" / {trunc}" if trunc else "")),
            ("備考", notes),
        ]
        bold = wb.add_format({"bold": True, "valign": "top"})
        wrap = wb.add_format({"text_wrap": True, "valign": "top"})
        ws_note.set_column(0, 0, 16)
        ws_note.set_column(1, 1, 90)
        for i, (k, v) in enumerate(info):
            ws_note.write_string(i, 0, k, bold)
            ws_note.write_string(i, 1, str(v or ""), wrap)
        wb.close()
        if not overwrite:
            target = _unique_path(target)
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    lines = [
        f"Wrote {target} ({target.stat().st_size} bytes)",
        f"sheets: データ ({len(df)} rows x {len(df.columns)} cols), グラフ ({kind} chart), 説明"
        + (", 集計" if summary_sheet else ""),
        f"chart reads from sheet '{ref_sheet}'; x={x}  y={', '.join(ys)}  group={group or '-'}",
    ]
    if trunc:
        lines.append(f"note: {trunc}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- chart_from_python

_RUNNER = r'''
import os, sys, traceback
os.environ.setdefault("MPLBACKEND", "Agg")
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
_have = {f.name for f in font_manager.fontManager.ttflist}
for _c in __FONTS__:
    if _c in _have:
        matplotlib.rcParams["font.family"] = [_c]
        break
matplotlib.rcParams["axes.unicode_minus"] = False
OUT_PATH = os.environ["CHART_OUT_PATH"]
DATA_PATH = os.environ.get("CHART_DATA_PATH") or None
_g = {"__name__": "__main__", "OUT_PATH": OUT_PATH, "DATA_PATH": DATA_PATH}
try:
    with open(os.environ["CHART_USER_CODE"], encoding="utf-8") as _f:
        _src = _f.read()
    exec(compile(_src, "chart_code.py", "exec"), _g)
    if not os.path.exists(OUT_PATH) and plt.get_fignums():
        plt.savefig(OUT_PATH, dpi=150)
except SystemExit:
    raise
except BaseException:
    traceback.print_exc()
    sys.exit(1)
'''

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _tail(text: str, n: int = 1500) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else "..." + text[-n:]


def chart_from_python(
    code: str,
    data_path: str = "",
    out_path: str = "",
    timeout: int = 60,
) -> str:
    """Run matplotlib code in a child process and return the PNG it draws.

    ARBITRARY CODE EXECUTION. Unlike chart_render, this runs whatever python it is given with
    this user's permissions (files, network, processes). It is therefore GATED: it works only
    after unlock(password=...), exactly like run_python. No attempt is made to detect harmful
    code; use chart_render / excel_chart_export when a standard chart is enough.

    Execution: a child python process (this server's interpreter), working directory
    %TEMP%\\m365-companion\\chart_from_python\\<id> (deleted afterwards), hard timeout.
    The code sees two variables: OUT_PATH (where the PNG must end up) and DATA_PATH (the
    data_path argument, or None). Write the figure with plt.savefig(OUT_PATH); if the code
    leaves a figure open and never saves, it is saved to OUT_PATH for you. The Agg backend
    is set and a Japanese font is selected when one is installed.

    Args:
        code: Python source (matplotlib / pandas / numpy / openpyxl are available).
        data_path: Optional CSV/xlsx under the allowed base, exposed to the code as DATA_PATH.
        out_path: Output .png path. Default: a file under the temp home. An existing file is
            never overwritten: the name gets _2, _3 ... and the real path is returned.
        timeout: Seconds before the child is killed (default 60, max 600).

    Returns: the PNG path plus the tail of stdout/stderr, or "[chart_from_python error: ...]"
    (also when no PNG appeared at OUT_PATH).
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        return _chart_from_python(code, data_path, out_path, timeout)
    except Exception as e:
        return _fail("chart_from_python", e)


def _chart_from_python(code: str, data_path: str, out_path: str, timeout: int) -> str:
    import subprocess
    from . import childproc

    if not (code or "").strip():
        raise _ToolError("code is empty")
    _need("matplotlib")
    try:
        timeout = max(1, min(int(timeout), 600))
    except (TypeError, ValueError):
        timeout = 60
    out = _out_path(out_path, "chart_py", ".png")
    if out.suffix.lower() != ".png":
        raise _ToolError("out_path must end with .png")
    out = _unique_path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    data = ""
    if data_path:
        dp = _validate_path(data_path)
        if not dp.is_file():
            raise _ToolError(f"data_path not found: {dp}")
        data = str(dp)

    try:
        from relay.temp_home import temp_dir
        workdir = Path(temp_dir(f"chart_from_python/{uuid.uuid4().hex[:12]}"))
    except Exception:
        import tempfile
        workdir = Path(tempfile.gettempdir()) / "m365-companion" / "chart_from_python" / uuid.uuid4().hex[:12]
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        (workdir / "runner.py").write_text(
            _RUNNER.replace("__FONTS__", repr(JP_FONT_CANDIDATES)), encoding="utf-8")
        (workdir / "chart_code.py").write_text(code, encoding="utf-8")
        env = dict(os.environ)
        env.update({
            "MPLBACKEND": "Agg",
            "MPLCONFIGDIR": str(workdir / "mplconfig"),
            "CHART_OUT_PATH": str(out),
            "CHART_DATA_PATH": data,
            "CHART_USER_CODE": str(workdir / "chart_code.py"),
            "PYTHONIOENCODING": "utf-8",
        })
        kw: dict = {}
        if os.name == "nt":
            kw["creationflags"] = childproc.headless_creationflags()
        try:
            proc = childproc.run(
                [sys.executable, str(workdir / "runner.py")],
                cwd=str(workdir), env=env, timeout=timeout,
                stdin=subprocess.DEVNULL, **kw)
        except subprocess.TimeoutExpired:
            raise _ToolError(f"timed out after {timeout}s; the child process was killed")
        stdout, stderr = _tail(proc.stdout), _tail(proc.stderr)
        if proc.returncode != 0:
            raise _ToolError(f"the code failed (exit {proc.returncode})\n"
                             f"--- stderr (tail) ---\n{stderr}\n--- stdout (tail) ---\n{stdout}")
        if not out.exists() or out.stat().st_size == 0:
            raise _ToolError("the code finished but no PNG was written to OUT_PATH "
                             f"({out}); call plt.savefig(OUT_PATH)\n"
                             f"--- stdout (tail) ---\n{stdout}\n--- stderr (tail) ---\n{stderr}")
        with open(out, "rb") as f:
            if f.read(8) != _PNG_MAGIC:
                raise _ToolError(f"{out} was written but is not a PNG file")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    lines = [f"Wrote {out} ({out.stat().st_size} bytes)"]
    if stdout:
        lines.append(f"--- stdout (tail) ---\n{stdout}")
    if stderr:
        lines.append(f"--- stderr (tail) ---\n{stderr}")
    return "\n".join(lines)
