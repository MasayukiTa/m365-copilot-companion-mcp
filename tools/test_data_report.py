"""Hermetic tests for the 3-tool NL-report pipeline (tools/data_report.py).

data_report_plan / data_report_run / data_report_explain never touch a real DB
or write real files here: odbc_query / odbc_to_excel / write_file / find_db_objects
/ procedural_memory_search are all monkeypatched with canned strings, and only
FAKE identifiers are used (fake_table_a, demo_db, ...) per repo policy.

Run: pytest -q tools\\test_data_report.py
"""
from __future__ import annotations

import json

import pytest

from tools import data_report as dr
from tools import data_discovery
from tools import procedural_memory as pm
from tools import odbc_ops
from tools import file_ops


@pytest.fixture(autouse=True)
def _bypass_unlock(monkeypatch):
    """data_report_run is mutating (gated); tests run with no HTTP request
    context at all, so without this every gated call would return a
    "[locked: no HTTP request context]" string instead of exercising the tool."""
    monkeypatch.setattr(dr, "require_unlocked", lambda: None)


# ===========================================================================
# pure helpers
# ===========================================================================


def test_slug_ascii_purpose():
    assert dr._slug("Monthly Sales Summary") == "monthly_sales_summary"


def test_slug_japanese_only_purposes_do_not_collide():
    # Neither string contains any ASCII letters/digits, so the ascii_slug fast
    # path degenerates to empty for both -- must fall back to a distinct
    # sha1-suffixed slug per purpose, not silently collapse to the same key.
    s1 = dr._slug("月次の売上集計")
    s2 = dr._slug("別の日本語の目的")
    assert s1 != s2
    assert s1.startswith("step_")
    assert s2.startswith("step_")


def test_slug_never_raises_on_non_string():
    assert dr._slug(None) == "step"
    assert dr._slug("") == "step"


def test_count_rows_parses_trailing_summary_line():
    text = "col1  col2\n----  ----\na     1\n--- 1 row(s)"
    assert dr._count_rows(text) == 1


def test_count_rows_returns_none_on_error_string():
    assert dr._count_rows("[odbc_query error: RuntimeError: no driver]") is None


def test_count_rows_returns_none_on_no_result_set():
    assert dr._count_rows("(statement returned no result set)") is None


def test_extract_where_pulls_clause_up_to_group_by():
    sql = "SELECT a FROM FAKE_TABLE_A WHERE status = 'active' GROUP BY a"
    where = dr._extract_where(sql)
    assert where == "WHERE status = 'active'"


def test_extract_where_none_when_absent():
    assert dr._extract_where("SELECT a FROM FAKE_TABLE_A") is None


def test_extract_where_never_raises_on_bad_input():
    assert dr._extract_where(None) is None
    assert dr._extract_where(123) is None


def test_extract_period_finds_between_range():
    sql = "SELECT * FROM FAKE_TABLE_A WHERE d BETWEEN '2026-01-01' AND '2026-01-31'"
    period = dr._extract_period(sql)
    assert "BETWEEN" in period
    assert "2026-01-01" in period


def test_extract_period_finds_bare_date_literals():
    sql = "SELECT * FROM FAKE_TABLE_A WHERE d = '2026-07-01'"
    assert dr._extract_period(sql) == "2026-07-01"


def test_extract_period_none_when_absent():
    assert dr._extract_period("SELECT * FROM FAKE_TABLE_A") is None


def test_extract_exclusions_finds_not_in():
    sql = "SELECT * FROM FAKE_TABLE_A WHERE status NOT IN ('deleted','test')"
    ex = dr._extract_exclusions(sql)
    assert "NOT IN" in ex


def test_extract_exclusions_none_when_absent():
    assert dr._extract_exclusions("SELECT * FROM FAKE_TABLE_A") is None


# ===========================================================================
# _parse_find_db_objects -- must handle every _compose_answer shape
# ===========================================================================


def test_parse_find_db_objects_memory_hit_shape():
    text = (
        "question: where are customers\n"
        "connection: demo_db\n"
        "候補テーブル (memory):\n"
        "  - fake_table_a\n"
        "  - fake_view_b\n"
        "根拠 (memory intents/snippets):\n"
        "  - 顧客テーブルの場所: スニペット (fake data only)\n"
        "出典: memo.md\n"
    )
    tables, hits, warnings = dr._parse_find_db_objects(text)
    assert tables == ["fake_table_a", "fake_view_b"]
    assert any("顧客テーブルの場所" in h for h in hits)
    assert warnings == []


def test_parse_find_db_objects_live_fallback_shape():
    text = (
        "question: where are orders\n"
        "connection: demo_db\n"
        "memoryに該当なし。ライブ探索します:\n"
        f"{'catalog':<14}  {'schema':<14}  {'name':<30}  type\n"
        f"{'fakecat':<14}  {'dbo':<14}  {'fake_live_table':<30}  TABLE\n"
        "--- 1 object(s)\n"
    )
    tables, hits, warnings = dr._parse_find_db_objects(text)
    assert tables == ["fake_live_table"]
    assert any("ライブ探索します" in w for w in warnings)


def test_parse_find_db_objects_error_string_is_a_single_warning():
    tables, hits, warnings = dr._parse_find_db_objects("[find_db_objects error: RuntimeError: boom]")
    assert tables == []
    assert hits == []
    assert warnings == ["[find_db_objects error: RuntimeError: boom]"]


def test_parse_find_db_objects_never_raises_on_bad_input():
    assert dr._parse_find_db_objects(None) == ([], [], [])
    assert dr._parse_find_db_objects(123) == ([], [], [])


# ===========================================================================
# _prefilled_steps_from_memory
# ===========================================================================


CANNED_SQL_MEMORY_HIT = (
    "1 match(es) for 'monthly sales connection:demo_db':\n"
    "- [monthly_sales] score=3 tags=[table:fake_table_a,connection:demo_db] "
    "intent='月次売上集計'\n"
    "    SELECT month, SUM(amount) FROM FAKE_TABLE_A GROUP BY month"
)


def test_prefilled_steps_from_memory_extracts_sql():
    steps = dr._prefilled_steps_from_memory(CANNED_SQL_MEMORY_HIT)
    assert len(steps) == 1
    assert steps[0]["purpose"] == "月次売上集計"
    assert "SELECT" in steps[0]["sql"]
    assert steps[0]["kind"] == "aggregate"


def test_prefilled_steps_from_memory_empty_on_no_matches():
    assert dr._prefilled_steps_from_memory("(no matches)") == []


def test_prefilled_steps_from_memory_ignores_non_sql_snippets():
    text = (
        "1 match(es) for 'x':\n"
        "- [howto] score=1 tags=[] intent='some howto'\n"
        "    just a recipe, not sql"
    )
    assert dr._prefilled_steps_from_memory(text) == []


# ===========================================================================
# data_report_plan
# ===========================================================================


def test_plan_uses_find_db_objects_and_scaffolds_when_no_prefilled_sql(monkeypatch):
    canned = (
        "question: where are customers\n"
        "connection: demo_db\n"
        "候補テーブル (memory):\n"
        "  - fake_table_a\n"
        "根拠 (memory intents/snippets):\n"
        "  - 顧客テーブルの場所: スニペット\n"
    )
    monkeypatch.setattr(data_discovery, "find_db_objects", lambda q, c="", limit=8: canned)
    monkeypatch.setattr(pm, "procedural_memory_search", lambda query, limit=10: "(no matches)")

    out = dr.data_report_plan("where are customers", connection="demo_db")
    plan = json.loads(out)

    assert plan["question"] == "where are customers"
    assert plan["connection"] == "demo_db"
    assert "fake_table_a" in plan["tables"]
    assert any("顧客テーブルの場所" in h for h in plan["memory_hits"])
    assert plan["steps"] == [{"purpose": "（集計SQLをここに記入）", "sql": "", "kind": "aggregate"}]
    assert plan["outputs"] == ["markdown", "xlsx"]


def test_plan_prefills_sql_from_procedural_memory(monkeypatch):
    monkeypatch.setattr(data_discovery, "find_db_objects", lambda q, c="", limit=8: "question: x\n")
    monkeypatch.setattr(pm, "procedural_memory_search", lambda query, limit=10: CANNED_SQL_MEMORY_HIT)

    out = dr.data_report_plan("monthly sales", connection="demo_db")
    plan = json.loads(out)

    assert len(plan["steps"]) == 1
    assert "SELECT" in plan["steps"][0]["sql"]
    assert plan["steps"][0]["purpose"] == "月次売上集計"


def test_plan_rejects_empty_question():
    out = dr.data_report_plan("")
    plan = json.loads(out)
    assert plan["question"] == ""
    assert any("non-empty string" in w for w in plan["warnings"])


def test_plan_never_raises_when_find_db_objects_explodes(monkeypatch):
    def _boom(q, c="", limit=8):
        raise RuntimeError("boom")

    monkeypatch.setattr(data_discovery, "find_db_objects", _boom)
    monkeypatch.setattr(pm, "procedural_memory_search", lambda query, limit=10: "(no matches)")

    out = dr.data_report_plan("anything", connection="demo_db")
    plan = json.loads(out)  # must still be valid JSON
    assert plan["steps"]
    assert any("find_db_objects" in w for w in plan["warnings"])


# ===========================================================================
# data_report_run
# ===========================================================================


def test_run_rejects_plan_with_no_connection():
    plan = {"question": "q", "connection": "", "steps": [{"purpose": "p", "sql": "SELECT 1", "kind": "aggregate"}]}
    out = dr.data_report_run(plan, "C:/whatever")
    assert out.startswith("[data_report_run error")
    assert "connection" in out


def test_run_rejects_all_empty_sql(tmp_path):
    plan = {
        "question": "q", "connection": "demo_db",
        "steps": [
            {"purpose": "step one", "sql": "", "kind": "aggregate"},
            {"purpose": "step two", "sql": "   ", "kind": "verify"},
        ],
        "outputs": ["markdown"],
    }
    out = dr.data_report_run(plan, str(tmp_path))
    assert out.startswith("[data_report_run error: every step has empty sql")
    assert "step one" in out
    assert "step two" in out


def test_run_rejects_invalid_json_string_plan(tmp_path):
    out = dr.data_report_run("{not json", str(tmp_path))
    assert out == "[data_report_run error: plan is not valid JSON]"


def test_run_accepts_plan_as_json_string(monkeypatch, tmp_path):
    canned_result = "col1  col2\n----  ----\na     1\n--- 1 row(s)"
    monkeypatch.setattr(odbc_ops, "odbc_query", lambda connection, sql, **kw: canned_result)
    monkeypatch.setattr(odbc_ops, "odbc_to_excel", lambda connection, sql, output_path, **kw: f"Wrote {output_path} (1 rows, 2 cols)")

    written = []

    def _fake_write_file(path, content, encoding="utf-8"):
        written.append((path, content))
        return f"Wrote {path} ({len(content)} characters)"

    monkeypatch.setattr(file_ops, "write_file", _fake_write_file)

    plan = {
        "question": "monthly sales", "connection": "demo_db",
        "tables": ["fake_table_a"], "memory_hits": ["顧客テーブルの場所"],
        "steps": [{"purpose": "月次集計", "sql": "SELECT month, SUM(amount) FROM FAKE_TABLE_A GROUP BY month", "kind": "aggregate"}],
        "outputs": ["markdown", "xlsx"], "warnings": [],
    }
    out = dr.data_report_run(json.dumps(plan, ensure_ascii=False), str(tmp_path))

    assert "report generated" in out
    assert len(written) == 2  # report.md, report_manifest.json


def test_run_good_step_writes_report_and_manifest_and_calls_excel(monkeypatch, tmp_path):
    canned_result = "col1  col2\n----  ----\na     1\n--- 1 row(s)"
    excel_calls = []

    def _fake_odbc_to_excel(connection, sql, output_path, **kw):
        excel_calls.append((connection, sql, output_path))
        return f"Wrote {output_path} (1 rows, 2 cols)"

    monkeypatch.setattr(odbc_ops, "odbc_query", lambda connection, sql, **kw: canned_result)
    monkeypatch.setattr(odbc_ops, "odbc_to_excel", _fake_odbc_to_excel)

    written = []

    def _fake_write_file(path, content, encoding="utf-8"):
        written.append((path, content))
        return f"Wrote {path} ({len(content)} characters)"

    monkeypatch.setattr(file_ops, "write_file", _fake_write_file)

    plan = {
        "question": "monthly sales for FAKE_TABLE_A", "connection": "demo_db",
        "tables": ["fake_table_a"], "memory_hits": [],
        "steps": [{
            "purpose": "月次集計",
            "sql": "SELECT month, SUM(amount) FROM FAKE_TABLE_A WHERE status = 'active' GROUP BY month",
            "kind": "aggregate",
        }],
        "outputs": ["markdown", "xlsx"], "warnings": [],
    }
    out = dr.data_report_run(plan, str(tmp_path))

    assert "report generated" in out
    assert "月次集計: 1" in out
    assert len(excel_calls) == 1
    assert excel_calls[0][0] == "demo_db"
    assert str(tmp_path) in excel_calls[0][2]
    assert excel_calls[0][2].endswith(".xlsx")

    assert len(written) == 2
    report_path, report_content = written[0]
    assert report_path.endswith("report.md")
    assert "# monthly sales for FAKE_TABLE_A" in report_content
    assert "## 根拠と前提" in report_content
    assert "```sql" in report_content
    assert "WHERE status = 'active'" in report_content  # provenance WHERE line

    manifest_path, manifest_content = written[1]
    assert manifest_path.endswith("report_manifest.json")
    manifest = json.loads(manifest_content)
    assert manifest["connection"] == "demo_db"
    assert manifest["queries"][0]["rows"] == 1
    assert manifest["queries"][0]["output"].endswith(".xlsx")
    assert "generated_at" in manifest


def test_run_tolerates_one_bad_step_and_continues(monkeypatch, tmp_path):
    def _fake_odbc_query(connection, sql, **kw):
        if "BAD" in sql:
            return "[odbc_query error: pyodbc.Error: table not found]"
        return "col1\n----\na\n--- 1 row(s)"

    monkeypatch.setattr(odbc_ops, "odbc_query", _fake_odbc_query)
    monkeypatch.setattr(odbc_ops, "odbc_to_excel", lambda connection, sql, output_path, **kw: f"Wrote {output_path} (1 rows, 1 cols)")

    written = []
    monkeypatch.setattr(
        file_ops, "write_file",
        lambda path, content, encoding="utf-8": (written.append((path, content)), f"Wrote {path}")[1],
    )

    plan = {
        "question": "q", "connection": "demo_db", "tables": [], "memory_hits": [],
        "steps": [
            {"purpose": "good step", "sql": "SELECT 1 FROM FAKE_TABLE_A", "kind": "aggregate"},
            {"purpose": "bad step", "sql": "SELECT BAD FROM FAKE_TABLE_A", "kind": "aggregate"},
        ],
        "outputs": ["markdown", "xlsx"], "warnings": [],
    }
    out = dr.data_report_run(plan, str(tmp_path))

    assert "report generated" in out
    assert "bad step: (unknown)" in out
    assert "good step: 1" in out
    assert "警告" in out

    manifest_content = written[-1][1]
    manifest = json.loads(manifest_content)
    rows_by_purpose = {q["purpose"]: q["rows"] for q in manifest["queries"]}
    assert rows_by_purpose["good step"] == 1
    assert rows_by_purpose["bad step"] is None
    assert any("bad step" in w for w in manifest["warnings"])


# ===========================================================================
# data_report_explain
# ===========================================================================


def test_explain_always_includes_all_labeled_sections_with_no_entry_fallbacks():
    out = dr.data_report_explain({}, "")
    assert "使用テーブル: （記載なし）" in out
    assert "抽出条件(WHERE): （記載なし）" in out
    assert "対象期間: （記載なし）" in out
    assert "除外条件: （記載なし）" in out
    assert "件数:" in out
    assert "注意点: （記載なし）" in out


def test_explain_fills_where_period_exclusions_from_plan_steps():
    plan = {
        "tables": ["fake_table_a", "fake_view_b"],
        "steps": [{
            "purpose": "月次集計",
            "sql": (
                "SELECT * FROM FAKE_TABLE_A WHERE d BETWEEN '2026-01-01' AND '2026-01-31' "
                "AND status NOT IN ('deleted')"
            ),
            "kind": "aggregate",
        }],
        "warnings": ["注意: サンプルデータのみ"],
    }
    out = dr.data_report_explain(plan, {"queries": [{"purpose": "月次集計", "rows": 42}]})
    assert "fake_table_a" in out and "fake_view_b" in out
    assert "BETWEEN" in out
    assert "2026-01-01" in out
    assert "NOT IN" in out
    assert "月次集計: 42" in out
    assert "サンプルデータのみ" in out


def test_explain_accepts_manifest_as_json_string():
    plan = {"tables": [], "steps": [{"purpose": "p", "sql": "SELECT 1", "kind": "aggregate"}], "warnings": []}
    manifest_json = json.dumps({"queries": [{"purpose": "p", "rows": 7}]})
    out = dr.data_report_explain(plan, manifest_json)
    assert "p: 7" in out


def test_explain_never_raises_on_invalid_plan():
    out = dr.data_report_explain("not json at all", "")
    assert "plan is not valid JSON" in out
    assert "使用テーブル" in out


def test_explain_never_raises_on_none_plan():
    out = dr.data_report_explain(None, "")
    assert "使用テーブル" in out
