"""3-tool NL-report pipeline: plan -> run -> explain.

MISSION: make "explore -> aggregate -> analyze -> output" robust EVEN WHEN THE
MODEL IS WEAK, by putting the STRUCTURE and PROVENANCE in Python (deterministic)
instead of hoping a weak model invents good structure every time. The MCP server
has NO LLM -- these tools do NOT generate SQL from scratch. Instead:

  data_report_plan  -- assembles a FROZEN plan (JSON) from memory (find_db_objects
                        + procedural_memory_search), pre-filling SQL steps when a
                        past successful query for a similar question exists, and
                        always leaving an explicit scaffold slot for the agent to
                        fill when nothing is pre-fillable. Read-only, ungated.
  data_report_run   -- executes the plan's steps deterministically via odbc_query
                        / odbc_to_excel, tolerating a bad step (one failure must
                        not abort the whole report), and writes report.md +
                        report_manifest.json (+ optional .docx/.pptx). Mutating,
                        gated by require_unlocked().
  data_report_explain -- composes the provenance block (used tables / WHERE /
                        period / exclusions / row counts / caveats) that is
                        ALWAYS present in the report body, even when a value is
                        unknown ("(no entry)"). Read-only, ungated.

This module does not implement ODBC/Excel/docx/pptx itself -- it calls the
existing gateway tools (tools/odbc_ops.py, tools/docx_ops.py, tools/pptx_ops.py,
tools/file_ops.py) exactly the way tools/data_discovery.py calls odbc_tables:
lazy-imported inside the function that needs it, so this module stays import-safe
on a CI box with no ODBC driver / no pyodbc installed. Module top-level imports
are stdlib + tools.security only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Optional

from .security import require_unlocked

# ===========================================================================
# pure helpers -- unit-tested directly with canned strings, no DB/files
# ===========================================================================

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(purpose: str) -> str:
    """Turn a step purpose into a filesystem-safe, collision-resistant slug.

    ASCII fast path: lowercase, non-alnum -> '_'. Purposes that are entirely
    non-ASCII (e.g. a Japanese-only scaffold purpose) would otherwise all
    collapse to the same empty string and silently overwrite each other's
    .xlsx output -- mirrors the sha1-fallback fix already applied in
    tools/procedural_memory.py's _slugify for the same reason.
    """
    text = purpose if isinstance(purpose, str) and purpose.strip() else "step"
    ascii_slug = _SLUG_RE.sub("_", text.strip().lower()).strip("_")
    if ascii_slug:
        return ascii_slug[:60]
    digest = hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:8]
    return f"step_{digest}"


_ROW_COUNT_RE = re.compile(r"---\s*(\d+)\s*row\(s\)")


def _count_rows(text: str) -> Optional[int]:
    """Parse the trailing '--- N row(s)' summary line odbc_query's _format_table
    always appends. Returns None when not parseable (error string, no result
    set, or any other shape) -- callers must tolerate an unknown row count."""
    if not text or not isinstance(text, str):
        return None
    m = _ROW_COUNT_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


_CLAUSE_STOP_RE = re.compile(
    r"\b(GROUP\s+BY|ORDER\s+BY|HAVING|UNION|LIMIT|OFFSET)\b", re.IGNORECASE
)


def _extract_where(sql: str) -> Optional[str]:
    """Heuristically pull the WHERE ... clause substring out of one SQL step.

    Not a SQL parser: finds the first `WHERE` keyword and takes everything up
    to the next top-level clause keyword (GROUP BY / ORDER BY / HAVING / UNION
    / LIMIT / OFFSET) or the end of the statement. Good enough for provenance
    display; never raises, returns None when there is no WHERE clause."""
    try:
        if not sql or not isinstance(sql, str):
            return None
        m = re.search(r"\bWHERE\b", sql, re.IGNORECASE)
        if not m:
            return None
        rest = sql[m.end():]
        stop = _CLAUSE_STOP_RE.search(rest)
        clause = rest[: stop.start()] if stop else rest
        clause = clause.strip().rstrip(";").strip()
        return f"WHERE {clause}" if clause else None
    except Exception:
        return None


_BETWEEN_RE = re.compile(r"\bBETWEEN\b.+?\bAND\b\s*[^\s)]+", re.IGNORECASE | re.DOTALL)
_DATE_LITERAL_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PERIOD_HINT_RE = re.compile(r">=|<=|\bGETDATE\b|\bDATEADD\b|\bDATEDIFF\b", re.IGNORECASE)


def _extract_period(sql: str) -> Optional[str]:
    """Heuristically pull a "target period" hint out of one SQL step: a BETWEEN
    ... AND ... range first, else any YYYY-MM-DD date literals, else a small
    window around a >=/<=/GETDATE/DATEADD hint. Never raises; None if nothing
    period-shaped is found (caller must render "(no entry)")."""
    try:
        if not sql or not isinstance(sql, str):
            return None
        m = _BETWEEN_RE.search(sql)
        if m:
            return re.sub(r"\s+", " ", m.group(0)).strip()
        dates = _DATE_LITERAL_RE.findall(sql)
        if dates:
            return ", ".join(sorted(set(dates)))
        m2 = _PERIOD_HINT_RE.search(sql)
        if m2:
            start = max(0, m2.start() - 40)
            end = min(len(sql), m2.end() + 40)
            return re.sub(r"\s+", " ", sql[start:end]).strip()
        return None
    except Exception:
        return None


_EXCLUDE_RE = re.compile(r"\bNOT\s+IN\b|\bNOT\s+LIKE\b|<>|!=|\bEXCLUD\w*", re.IGNORECASE)


def _extract_exclusions(sql: str) -> Optional[str]:
    """Heuristically pull an "exclusion condition" hint out of one SQL step
    (NOT IN / NOT LIKE / <> / != / EXCLUDE*). Never raises; None if none found."""
    try:
        if not sql or not isinstance(sql, str):
            return None
        m = _EXCLUDE_RE.search(sql)
        if not m:
            return None
        start = max(0, m.start() - 30)
        end = min(len(sql), m.end() + 40)
        return re.sub(r"\s+", " ", sql[start:end]).strip()
    except Exception:
        return None


def _dedup_preserve_order(items: list) -> list:
    seen: set = set()
    out: list = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


_BULLET_RE = re.compile(r"^\s*-\s+(.*)$")
_CUE_WORDS = ("注意", "NG", "警告", "gotcha", "Gotcha", "失敗", "該当なし")


def _parse_find_db_objects(text: str) -> tuple[list[str], list[str], list[str]]:
    """Parse a find_db_objects() answer string into (tables, memory_hits, warnings).

    Tolerant of every shape _compose_answer (tools/data_discovery.py) can
    produce: a "候補テーブル (memory):" bullet block, a "根拠 (...)" bullet
    block, a "出典: ..." line, a live odbc_tables-style fallback table, an
    empty_reason guidance line, or a bare "[find_db_objects error ...]" string
    (treated as a single warning, no tables/hits). Never raises.
    """
    tables: list[str] = []
    hits: list[str] = []
    warnings: list[str] = []
    try:
        if not text or not isinstance(text, str):
            return tables, hits, warnings
        if text.startswith("["):
            return tables, hits, [text]

        section: Optional[str] = None
        for ln in text.splitlines():
            stripped = ln.strip()
            if not stripped:
                continue
            if stripped.startswith("候補テーブル"):
                section = "tables"
                continue
            if stripped.startswith("根拠"):
                section = "hits"
                continue
            if stripped.startswith("出典"):
                section = None
                hits.append(stripped)
                continue
            if stripped.startswith("memoryに該当なし") or stripped.startswith("procedural_memory_import_markdown"):
                section = "live"
                warnings.append(stripped)
                continue

            if section == "tables":
                m = _BULLET_RE.match(ln)
                if m:
                    tables.append(m.group(1).strip())
                    continue
                section = None
            if section == "hits":
                m = _BULLET_RE.match(ln)
                if m:
                    hits.append(m.group(1).strip())
                    continue
                section = None
            if section == "live":
                if stripped.lower().startswith("catalog"):
                    continue
                if stripped.startswith("---"):
                    continue
                parts = stripped.split()
                if len(parts) >= 4:
                    tables.append(parts[2])
                elif len(parts) == 1:
                    tables.append(parts[0])
                continue

            if any(w in stripped for w in _CUE_WORDS):
                warnings.append(stripped)

        return _dedup_preserve_order(tables), _dedup_preserve_order(hits), _dedup_preserve_order(warnings)
    except Exception as e:
        return [], [], [f"_parse_find_db_objects error: {type(e).__name__}: {e}"]


def _parse_columns_text(text: str) -> list[str]:
    """Pull column names out of an odbc_columns() text table. Skips the header
    row and the trailing '--- N column(s)' summary. Never raises."""
    cols: list[str] = []
    try:
        if not text or not isinstance(text, str):
            return cols
        lines = text.splitlines()
        for ln in lines[1:]:
            stripped = ln.strip()
            if not stripped or stripped.startswith("---"):
                continue
            parts = stripped.split()
            if parts:
                cols.append(parts[0])
        return cols
    except Exception:
        return cols


def _extract_memory_match_blocks(text: str) -> list[str]:
    """Split a procedural_memory_search() result into per-match blocks (each
    block starts at a "- [" line and runs to the next one, or end of text)."""
    blocks: list[str] = []
    current: list[str] = []
    for ln in text.splitlines():
        if ln.startswith("- ["):
            if current:
                blocks.append("\n".join(current))
            current = [ln]
        elif current:
            current.append(ln)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _intent_and_snippet_from_memory_block(block: str) -> tuple[str, str]:
    """Pull (intent, snippet) out of one procedural_memory_search match block."""
    lines = block.splitlines()
    first_line, rest = (lines[0], lines[1:]) if lines else ("", [])
    intent = ""
    idx = first_line.find("intent=")
    if idx != -1:
        raw = first_line[idx + len("intent="):].strip()
        if len(raw) >= 2 and raw[0] in ("'", '"') and raw[-1] == raw[0]:
            raw = raw[1:-1]
        intent = raw
    snippet = "\n".join(ln.strip() for ln in rest).strip()
    return intent, snippet


def _prefilled_steps_from_memory(mem_result: str, max_steps: int = 3) -> list[dict]:
    """Pre-fill plan steps from past successful procedures whose stored snippet
    looks like SQL (contains SELECT) -- the "second time, no re-exploration"
    payoff: memory pre-fills the SQL instead of the agent re-deriving structure.
    Never raises; [] when mem_result has no matches / is an error string."""
    steps: list[dict] = []
    try:
        if not mem_result or not isinstance(mem_result, str):
            return steps
        if mem_result.startswith("(no matches") or mem_result.startswith("["):
            return steps
        for block in _extract_memory_match_blocks(mem_result):
            intent, snippet = _intent_and_snippet_from_memory_block(block)
            if snippet and "select" in snippet.lower():
                steps.append({
                    "purpose": intent or "(memoryから復元したSQL)",
                    "sql": snippet,
                    "kind": "aggregate",
                })
            if len(steps) >= max_steps:
                break
        return steps
    except Exception:
        return steps


def _coerce_plan(plan: Any) -> dict:
    """Accept a plan as dict or JSON string (both forms are frozen-schema
    valid). Raises ValueError on anything else so callers can turn that into
    a clear error message instead of an unhandled exception."""
    if isinstance(plan, dict):
        return plan
    if isinstance(plan, str):
        try:
            data = json.loads(plan)
        except Exception:
            raise ValueError("plan is not valid JSON")
        if not isinstance(data, dict):
            raise ValueError("plan is not valid JSON")
        return data
    raise ValueError("plan is not valid JSON")


def _minimal_plan(question: str, connection: str, warnings: list[str]) -> dict:
    """A minimal-but-schema-valid plan, used as the never-raise fallback."""
    return {
        "question": question,
        "connection": connection or "",
        "memory_hits": [],
        "tables": [],
        "columns": {},
        "steps": [{"purpose": "（集計SQLをここに記入）", "sql": "", "kind": "aggregate"}],
        "outputs": ["markdown", "xlsx"],
        "warnings": warnings,
    }


def _normalize_result_queries(plan: dict, result: Any) -> list[dict]:
    """Merge plan.steps (purpose/sql, the source of truth for what was asked)
    with any row counts found in `result` (a manifest dict/JSON, a
    {"queries": [...]} collected-results dict, or nothing). Never raises."""
    try:
        steps = plan.get("steps") or []
        if isinstance(result, str) and result.strip():
            try:
                result = json.loads(result)
            except Exception:
                result = None
        rows_by_purpose: dict[str, Any] = {}
        if isinstance(result, dict):
            queries = result.get("queries")
            if isinstance(queries, list):
                for q in queries:
                    if isinstance(q, dict):
                        rows_by_purpose[q.get("purpose", "")] = q.get("rows")
        out: list[dict] = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            purpose = step.get("purpose", "")
            sql = step.get("sql", "")
            out.append({"purpose": purpose, "sql": sql, "rows": rows_by_purpose.get(purpose)})
        return out
    except Exception:
        return []


_NO_ENTRY = "（記載なし）"


def _provenance_markdown(plan: dict, result: Any = "") -> str:
    """Compose the provenance markdown block. ALWAYS contains every labeled
    line, even when a value is unknown (rendered as _NO_ENTRY) -- this is the
    "根拠を必ず本文に入れる" guarantee the mission asks for. Never raises."""
    try:
        if not isinstance(plan, dict):
            plan = {}
        tables = plan.get("tables") or []
        queries = _normalize_result_queries(plan, result)

        tables_line = "、".join(str(t) for t in tables) if tables else _NO_ENTRY

        where_parts, period_parts, exclude_parts = [], [], []
        for q in queries:
            sql = q.get("sql") or ""
            purpose = q.get("purpose", "")
            w = _extract_where(sql)
            if w:
                where_parts.append(f"[{purpose}] {w}")
            p = _extract_period(sql)
            if p:
                period_parts.append(f"[{purpose}] {p}")
            ex = _extract_exclusions(sql)
            if ex:
                exclude_parts.append(f"[{purpose}] {ex}")

        where_line = "; ".join(where_parts) if where_parts else _NO_ENTRY
        period_line = "; ".join(period_parts) if period_parts else _NO_ENTRY
        exclude_line = "; ".join(exclude_parts) if exclude_parts else _NO_ENTRY

        if queries:
            count_lines = []
            for q in queries:
                rows = q.get("rows")
                rows_str = str(rows) if rows is not None else "（不明）"
                count_lines.append(f"  - {q.get('purpose', '')}: {rows_str}")
            count_block = "\n".join(count_lines)
        else:
            count_block = f"  - {_NO_ENTRY}"

        warnings = plan.get("warnings") or []
        warnings_line = "; ".join(str(w) for w in warnings) if warnings else _NO_ENTRY

        return "\n".join([
            f"- 使用テーブル: {tables_line}",
            f"- 抽出条件(WHERE): {where_line}",
            f"- 対象期間: {period_line}",
            f"- 除外条件: {exclude_line}",
            "- 件数:",
            count_block,
            f"- 注意点: {warnings_line}",
        ])
    except Exception as e:
        return f"- 使用テーブル: {_NO_ENTRY}\n- 注意点: _provenance_markdown error: {type(e).__name__}: {e}"


def _compose_manifest(
    question: str,
    connection: str,
    memory_hits: list,
    tables: list,
    queries: list,
    generated_files: list,
    warnings: list,
) -> dict:
    """Assemble the frozen report_manifest.json schema from already-collected
    pieces. Kept separate so it is unit-testable without any DB or files."""
    return {
        "question": question,
        "connection": connection,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "memory_hits": list(memory_hits or []),
        "tables": list(tables or []),
        "queries": list(queries or []),
        "generated_files": list(generated_files or []),
        "warnings": list(warnings or []),
    }


# ===========================================================================
# gateway tools
# ===========================================================================


def data_report_plan(question: str, connection: str = "", limit: int = 8) -> str:
    """Assemble a FROZEN report plan (JSON) from memory -- read-only, no unlock
    needed. Calls find_db_objects (memory-first table/column discovery) and
    procedural_memory_search (past successful SQL for a similar question), so a
    SECOND ask about a similar topic pre-fills SQL instead of re-exploring from
    scratch. Always includes at least one scaffold step ("（集計SQLをここに記入）")
    when nothing could be pre-filled, so a weak model has an explicit slot to
    fill rather than inventing structure. Never raises -- any internal failure
    degrades to a minimal-but-valid plan with a warning explaining what broke.

    Args:
        question: Free-text question the report should answer.
        connection: Optional named ODBC connection (biases memory search and,
            when tables were found, is used to look up a few columns live).
        limit: Max procedural_memory_search / find_db_objects matches to use.
    """
    try:
        if not question or not isinstance(question, str):
            return json.dumps(
                _minimal_plan("", connection, ["question must be a non-empty string"]),
                ensure_ascii=False, indent=2,
            )

        tables: list[str] = []
        memory_hits: list[str] = []
        warnings: list[str] = []
        try:
            from .data_discovery import find_db_objects
            discovery_text = find_db_objects(question, connection, limit)
            tables, memory_hits, warnings = _parse_find_db_objects(discovery_text)
        except Exception as e:
            warnings.append(f"find_db_objects unavailable: {type(e).__name__}: {e}")

        columns: dict[str, list[str]] = {}
        if connection and tables:
            try:
                from .odbc_ops import odbc_columns
                for t in tables[:3]:
                    try:
                        col_text = odbc_columns(connection, t)
                    except Exception:
                        continue
                    if not isinstance(col_text, str) or col_text.startswith("["):
                        continue
                    cols = _parse_columns_text(col_text)
                    if cols:
                        columns[t] = cols
            except Exception:
                pass

        steps: list[dict] = []
        try:
            from .procedural_memory import procedural_memory_search
            mem_query = f"{question} connection:{connection}" if connection else question
            mem_result = procedural_memory_search(mem_query, limit)
            steps = _prefilled_steps_from_memory(mem_result)
        except Exception:
            steps = []

        if not steps:
            steps = [{"purpose": "（集計SQLをここに記入）", "sql": "", "kind": "aggregate"}]

        plan = {
            "question": question,
            "connection": connection or "",
            "memory_hits": memory_hits,
            "tables": tables,
            "columns": columns,
            "steps": steps,
            "outputs": ["markdown", "xlsx"],
            "warnings": warnings,
        }
        return json.dumps(plan, ensure_ascii=False, indent=2)
    except Exception as e:
        q = question if isinstance(question, str) else ""
        return json.dumps(
            _minimal_plan(q, connection, [f"data_report_plan error: {type(e).__name__}: {e}"]),
            ensure_ascii=False, indent=2,
        )


def data_report_run(plan: Any, output_dir: str) -> str:
    """Execute a report plan deterministically and write report.md +
    report_manifest.json (+ optional .docx/.pptx). Mutating -- requires unlock.

    Accepts `plan` as a dict OR a JSON string (as returned by data_report_plan).
    Rejects a plan with no connection or with every step's sql empty (does NOT
    fabricate SQL -- that is the agent's job to fill in first). One bad step
    (odbc_query returning an "[... error ...]" string) is recorded as a warning
    and does not abort the rest of the report.

    Args:
        plan: dict or JSON string matching the frozen plan schema.
        output_dir: Directory to write report.md / report_manifest.json (and
            any .xlsx/.docx/.pptx) into. Created if missing.
    """
    locked = require_unlocked()
    if locked:
        return locked
    try:
        try:
            plan_dict = _coerce_plan(plan)
        except ValueError:
            return "[data_report_run error: plan is not valid JSON]"

        connection = plan_dict.get("connection") or ""
        if not connection:
            return "[data_report_run error: plan.connection is required]"

        steps = plan_dict.get("steps")
        if not isinstance(steps, list) or not steps:
            return "[data_report_run error: plan.steps must be a non-empty list]"

        empty_purposes = [
            (s.get("purpose") or "(no purpose)") for s in steps
            if isinstance(s, dict) and not (s.get("sql") or "").strip()
        ]
        if len(empty_purposes) == len(steps):
            listing = "; ".join(empty_purposes)
            return (
                "[data_report_run error: every step has empty sql -- fill in the SQL "
                f"before calling data_report_run. Empty steps: {listing}]"
            )

        question = plan_dict.get("question") or ""
        tables = plan_dict.get("tables") or []
        memory_hits = plan_dict.get("memory_hits") or []
        outputs = plan_dict.get("outputs") or ["markdown"]
        run_warnings: list[str] = list(plan_dict.get("warnings") or [])

        from .file_ops import _validate_path
        out_dir = _validate_path(output_dir)
        os.makedirs(out_dir, exist_ok=True)

        queries_manifest: list[dict] = []
        step_previews: list[dict] = []

        for step in steps:
            if not isinstance(step, dict):
                continue
            purpose = step.get("purpose") or "(no purpose)"
            sql = (step.get("sql") or "").strip()
            kind = step.get("kind") or "aggregate"

            if not sql:
                queries_manifest.append({"purpose": purpose, "sql": "", "rows": None, "output": None})
                step_previews.append({"purpose": purpose, "sql": "", "preview": "(SQL未指定のためスキップ)"})
                continue

            if kind in ("aggregate", "detail") and "xlsx" in outputs:
                # SINGLE DB round-trip for this step: one execution produces
                # BOTH the report.md preview and the xlsx, from the same
                # fetched result set (fixes the old double-execution bug where
                # odbc_query() and odbc_to_excel() each ran the same SQL
                # separately, which could disagree on a live-updating DB).
                xlsx_path = out_dir / f"{_slug(purpose)}.xlsx"
                try:
                    from .odbc_ops import odbc_query_to_excel_and_preview
                    combined = odbc_query_to_excel_and_preview(connection, sql, str(xlsx_path))
                except Exception as e:
                    combined = {"ok": False, "error": f"[odbc_query_to_excel_and_preview error: {type(e).__name__}: {e}]"}

                if not isinstance(combined, dict) or not combined.get("ok"):
                    err = combined.get("error") if isinstance(combined, dict) else str(combined)
                    run_warnings.append(f"{purpose}: {err}")
                    queries_manifest.append({"purpose": purpose, "sql": sql, "rows": None, "output": None})
                    step_previews.append({"purpose": purpose, "sql": sql, "preview": str(err)})
                    continue

                queries_manifest.append({
                    "purpose": purpose, "sql": sql,
                    "rows": combined.get("rows"), "output": combined.get("xlsx"),
                })
                step_previews.append({"purpose": purpose, "sql": sql, "preview": combined.get("preview") or ""})
                continue

            try:
                from .odbc_ops import odbc_query
                result_text = odbc_query(connection, sql)
            except Exception as e:
                result_text = f"[odbc_query error: {type(e).__name__}: {e}]"

            if not isinstance(result_text, str) or result_text.startswith("["):
                run_warnings.append(f"{purpose}: {result_text}")
                queries_manifest.append({"purpose": purpose, "sql": sql, "rows": None, "output": None})
                step_previews.append({"purpose": purpose, "sql": sql, "preview": str(result_text)})
                continue

            rows = _count_rows(result_text)
            queries_manifest.append({"purpose": purpose, "sql": sql, "rows": rows, "output": None})
            preview = "\n".join(result_text.splitlines()[:15])
            step_previews.append({"purpose": purpose, "sql": sql, "preview": preview})

        provenance_md = data_report_explain(plan_dict, {"queries": queries_manifest})

        md_lines = [f"# {question}", "", "## 根拠と前提", "", provenance_md, ""]
        for sp in step_previews:
            md_lines.append(f"### {sp['purpose']}")
            md_lines.append("")
            md_lines.append("```sql")
            md_lines.append(sp["sql"] or "(SQL未指定)")
            md_lines.append("```")
            md_lines.append("")
            if sp["preview"]:
                md_lines.append("```")
                md_lines.append(sp["preview"])
                md_lines.append("```")
                md_lines.append("")
        report_md = "\n".join(md_lines)

        from .file_ops import write_file
        generated_files: list[str] = []
        report_md_path = out_dir / "report.md"
        write_result = write_file(str(report_md_path), report_md)
        if isinstance(write_result, str) and not write_result.startswith("["):
            generated_files.append(str(report_md_path))
        else:
            run_warnings.append(f"report.md: {write_result}")

        for q in queries_manifest:
            if q.get("output"):
                generated_files.append(q["output"])

        if "docx" in outputs:
            try:
                from .docx_ops import docx_from_markdown
                docx_path = out_dir / "report.docx"
                docx_result = docx_from_markdown(str(docx_path), report_md, title=question)
                if isinstance(docx_result, str) and not docx_result.startswith("["):
                    generated_files.append(str(docx_path))
                else:
                    run_warnings.append(f"report.docx: {docx_result}")
            except Exception as e:
                run_warnings.append(f"report.docx error: {type(e).__name__}: {e}")

        if "pptx" in outputs:
            try:
                from .pptx_ops import pptx_from_markdown
                pptx_path = out_dir / "report.pptx"
                pptx_result = pptx_from_markdown(str(pptx_path), report_md, title=question)
                if isinstance(pptx_result, str) and not pptx_result.startswith("["):
                    generated_files.append(str(pptx_path))
                else:
                    run_warnings.append(f"report.pptx: {pptx_result}")
            except Exception as e:
                run_warnings.append(f"report.pptx error: {type(e).__name__}: {e}")

        # The manifest self-lists ALL generated artifacts, including its own
        # path -- so build the complete list (report.md, any xlsx/docx/pptx,
        # AND report_manifest.json) BEFORE composing the manifest, then write
        # it. The run's return summary below uses this same complete list.
        manifest_path = out_dir / "report_manifest.json"
        generated_files.append(str(manifest_path))

        manifest = _compose_manifest(
            question=question, connection=connection, memory_hits=memory_hits,
            tables=tables, queries=queries_manifest, generated_files=list(generated_files),
            warnings=run_warnings,
        )
        manifest_write = write_file(str(manifest_path), json.dumps(manifest, ensure_ascii=False, indent=2))
        if not (isinstance(manifest_write, str) and not manifest_write.startswith("[")):
            run_warnings.append(f"report_manifest.json: {manifest_write}")

        lines = ["data_report_run: report generated", "", "生成ファイル (generated files):"]
        for f in generated_files:
            lines.append(f"  - {f}")
        lines.append("")
        lines.append("件数 (rows per step):")
        for q in queries_manifest:
            rows_str = q["rows"] if q["rows"] is not None else "(unknown)"
            lines.append(f"  - {q['purpose']}: {rows_str}")
        if run_warnings:
            lines.append("")
            lines.append("警告 (warnings):")
            for w in run_warnings:
                lines.append(f"  - {w}")
        lines.append("")
        lines.append(f"manifest: {manifest_path}")
        return "\n".join(lines)
    except Exception as e:
        return f"[data_report_run error: {type(e).__name__}: {e}]"


def data_report_explain(plan: Any, result: Any = "") -> str:
    """Compose the provenance markdown block for a plan -- read-only, no unlock
    needed, never raises. `result` may be a report_manifest dict/JSON string, a
    {"queries": [...]} collected-results dict (as data_report_run passes it), or
    "" (derive from plan.steps alone, with all row counts shown as unknown).

    Always emits every labeled line (使用テーブル / 抽出条件(WHERE) / 対象期間 /
    除外条件 / 件数 / 注意点), using "（記載なし）" for anything not determinable
    -- this is the guarantee that provenance is always in the report body, not
    left to a model's discretion.
    """
    try:
        try:
            plan_dict = _coerce_plan(plan)
        except ValueError:
            plan_dict = {"tables": [], "steps": [], "warnings": ["plan is not valid JSON"]}
        return _provenance_markdown(plan_dict, result)
    except Exception as e:
        return f"[data_report_explain error: {type(e).__name__}: {e}]"
