"""Hermetic tests for the DB-exploration auto-accumulation hook
(tools/data_memory_hook.py).

No DB, no network: record_query / record_tables / record_columns are called
directly with fake result_text strings that mimic what tools/odbc_ops.py
would have produced. procedural_memory_save is monkeypatched to a stub that
records its calls, so these tests never touch the real procedural memory
state file and never need an HTTP request context.

Only fake identifiers are used (demo_db, FAKE_TABLE_A, ...) -- see repo rule
against real internal DB/table names in committed test text.

Run: pytest -q tools\\test_data_memory_hook.py
"""
from __future__ import annotations

import pytest

from tools import data_memory_hook as hook


@pytest.fixture(autouse=True)
def _fake_save(monkeypatch):
    """Stub out procedural_memory.procedural_memory_save with a recorder.

    hook._save imports procedural_memory_save lazily inside the function
    body (`from .procedural_memory import procedural_memory_save`), so we
    patch it on the procedural_memory module itself -- that's what the
    lazy import resolves at call time.
    """
    calls = []

    def _fake(intent, snippet, tags="", context=""):
        calls.append({"intent": intent, "snippet": snippet, "tags": tags, "context": context})
        return f"saved procedure [{intent}] (fake)"

    monkeypatch.setattr("tools.procedural_memory.procedural_memory_save", _fake)
    return calls


@pytest.fixture(autouse=True)
def _auto_off_by_default(monkeypatch):
    """Default env to OFF for every test; individual tests opt in explicitly."""
    monkeypatch.delenv("MCP_DATA_MEMORY_AUTO", raising=False)


# ---------------------------------------------------------------------------
# (a) auto OFF -> no save calls for any of the 3 hooks
# ---------------------------------------------------------------------------


def test_auto_off_record_query_does_not_save(_fake_save):
    hook.record_query("demo_db", "SELECT * FROM FAKE_TABLE_A", "col\n---\na\n--- 1 row(s)")
    assert _fake_save == []


def test_auto_off_record_tables_does_not_save(_fake_save):
    listing = (
        f"{'catalog':<14}  {'schema':<14}  {'name':<30}  type\n"
        f"{'demo_db':<14}  {'dbo':<14}  {'FAKE_TABLE_A':<30}  TABLE\n"
        "--- 1 object(s)"
    )
    hook.record_tables("demo_db", listing)
    assert _fake_save == []


def test_auto_off_record_columns_does_not_save(_fake_save):
    listing = (
        f"{'column':<30}  {'type':<18}  size   nullable\n"
        f"{'ID':<30}  {'INT':<18}  {'10':<6}  NO\n"
        "--- 1 column(s)"
    )
    hook.record_columns("demo_db", "FAKE_TABLE_A", listing)
    assert _fake_save == []


# ---------------------------------------------------------------------------
# (b) auto ON + SUCCESS result_text -> save called with expected shape
# ---------------------------------------------------------------------------


def test_auto_on_record_query_success_saves_expected_shape(monkeypatch, _fake_save):
    monkeypatch.setenv("MCP_DATA_MEMORY_AUTO", "1")
    sql = "SELECT * FROM FAKE_TABLE_A"
    result_text = "id\n--\n1\n2\n--- 2 row(s)"

    hook.record_query("demo_db", sql, result_text)

    assert len(_fake_save) == 1
    call = _fake_save[0]
    assert call["intent"].startswith("SELECT * FROM FAKE_TABLE_A")
    assert call["snippet"] == sql
    assert "connection:demo_db" in call["tags"]
    assert "table:fake_table_a" in call["tags"]
    assert "db" in call["tags"]
    assert "auto" in call["tags"]
    assert "returned" in call["context"] and "lines" in call["context"]
    assert any(ch.isdigit() for ch in call["context"])  # a count is mentioned


def test_auto_on_record_tables_success_saves_expected_shape(monkeypatch, _fake_save):
    monkeypatch.setenv("MCP_DATA_MEMORY_AUTO", "1")
    listing = (
        f"{'catalog':<14}  {'schema':<14}  {'name':<30}  type\n"
        f"{'demo_db':<14}  {'dbo':<14}  {'FAKE_TABLE_A':<30}  TABLE\n"
        "--- 1 object(s)"
    )

    hook.record_tables("demo_db", listing)

    assert len(_fake_save) == 1
    call = _fake_save[0]
    assert call["intent"] == "tables in demo_db"
    assert "FAKE_TABLE_A" in call["snippet"]
    assert "connection:demo_db" in call["tags"]
    assert "catalog" in call["tags"]
    assert "1" in call["context"]


def test_auto_on_record_columns_success_saves_expected_shape(monkeypatch, _fake_save):
    monkeypatch.setenv("MCP_DATA_MEMORY_AUTO", "1")
    listing = (
        f"{'column':<30}  {'type':<18}  size   nullable\n"
        f"{'ID':<30}  {'INT':<18}  {'10':<6}  NO\n"
        f"{'NAME':<30}  {'VARCHAR':<18}  {'50':<6}  YES\n"
        "--- 2 column(s)"
    )

    hook.record_columns("demo_db", "FAKE_TABLE_A", listing)

    assert len(_fake_save) == 1
    call = _fake_save[0]
    assert call["intent"] == "columns of FAKE_TABLE_A (demo_db)"
    assert "ID" in call["snippet"]
    assert "NAME" in call["snippet"]
    assert "connection:demo_db" in call["tags"]
    assert "table:fake_table_a" in call["tags"]
    assert "2" in call["context"]


# ---------------------------------------------------------------------------
# (c) auto ON + ERROR result_text -> NO save
# ---------------------------------------------------------------------------


def test_auto_on_error_result_query_does_not_save(monkeypatch, _fake_save):
    monkeypatch.setenv("MCP_DATA_MEMORY_AUTO", "1")
    hook.record_query(
        "demo_db", "SELECT * FROM FAKE_TABLE_A",
        "[odbc_query error: OperationalError: connection refused]",
    )
    assert _fake_save == []


def test_auto_on_error_result_tables_does_not_save(monkeypatch, _fake_save):
    monkeypatch.setenv("MCP_DATA_MEMORY_AUTO", "1")
    hook.record_tables("demo_db", "[odbc_tables error: RuntimeError: boom]")
    assert _fake_save == []


def test_auto_on_error_result_columns_does_not_save(monkeypatch, _fake_save):
    monkeypatch.setenv("MCP_DATA_MEMORY_AUTO", "1")
    hook.record_columns("demo_db", "FAKE_TABLE_A", "[odbc_columns error: ValueError: bad]")
    assert _fake_save == []


# ---------------------------------------------------------------------------
# (d) auto ON but procedural_memory_save returns "[locked ...]" -> swallowed
# ---------------------------------------------------------------------------


def test_locked_save_response_is_swallowed_without_raising(monkeypatch):
    monkeypatch.setenv("MCP_DATA_MEMORY_AUTO", "1")

    def _locked(intent, snippet, tags="", context=""):
        return "[locked: no HTTP request context] Call unlock(password='<password>') first."

    monkeypatch.setattr("tools.procedural_memory.procedural_memory_save", _locked)

    result = hook.record_query(
        "demo_db", "SELECT * FROM FAKE_TABLE_A", "id\n--\n1\n--- 1 row(s)"
    )
    assert result is None  # swallowed quietly, no exception


def test_locked_save_response_swallowed_for_tables_and_columns(monkeypatch):
    monkeypatch.setenv("MCP_DATA_MEMORY_AUTO", "1")

    def _locked(intent, snippet, tags="", context=""):
        return "[locked: no HTTP request context] Call unlock(password='<password>') first."

    monkeypatch.setattr("tools.procedural_memory.procedural_memory_save", _locked)

    listing_tables = (
        f"{'catalog':<14}  {'schema':<14}  {'name':<30}  type\n"
        f"{'demo_db':<14}  {'dbo':<14}  {'FAKE_TABLE_A':<30}  TABLE\n"
        "--- 1 object(s)"
    )
    assert hook.record_tables("demo_db", listing_tables) is None

    listing_columns = (
        f"{'column':<30}  {'type':<18}  size   nullable\n"
        f"{'ID':<30}  {'INT':<18}  {'10':<6}  NO\n"
        "--- 1 column(s)"
    )
    assert hook.record_columns("demo_db", "FAKE_TABLE_A", listing_columns) is None


# ---------------------------------------------------------------------------
# (e) result "(no ... )" style -> no save
# ---------------------------------------------------------------------------


def test_no_tables_visible_does_not_save(monkeypatch, _fake_save):
    monkeypatch.setenv("MCP_DATA_MEMORY_AUTO", "1")
    hook.record_tables("demo_db", "(no tables visible to this user)")
    assert _fake_save == []


def test_no_columns_found_does_not_save(monkeypatch, _fake_save):
    monkeypatch.setenv("MCP_DATA_MEMORY_AUTO", "1")
    hook.record_columns("demo_db", "FAKE_TABLE_A", "(no columns found for 'FAKE_TABLE_A')")
    assert _fake_save == []


def test_no_result_set_does_not_save(monkeypatch, _fake_save):
    monkeypatch.setenv("MCP_DATA_MEMORY_AUTO", "1")
    hook.record_query(
        "demo_db", "EXEC some_proc", "(statement returned no result set)"
    )
    assert _fake_save == []


# ---------------------------------------------------------------------------
# extra: never raises even on odd input
# ---------------------------------------------------------------------------


def test_hooks_never_raise_on_empty_or_none_result(monkeypatch):
    monkeypatch.setenv("MCP_DATA_MEMORY_AUTO", "1")
    assert hook.record_query("demo_db", "SELECT 1", "") is None
    assert hook.record_tables("demo_db", "") is None
    assert hook.record_columns("demo_db", "FAKE_TABLE_A", "") is None
