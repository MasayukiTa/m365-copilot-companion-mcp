"""Hermetic tests for the data-discovery gateway tool (tools/data_discovery.py).

find_db_objects reads procedural memory FIRST (table:/src: tags + intents) and
only falls back to a live odbc_tables() listing when memory has nothing AND a
connection was supplied. These tests monkeypatch both procedural_memory_search
and odbc_tables so nothing touches a real state file, driver, or DB -- and use
only fake identifiers (fake_table_a, demo_db, etc).

Run: pytest -q tools\\test_data_discovery.py
"""
from __future__ import annotations

import pytest

from tools import data_discovery as dd
from tools import procedural_memory as pm
from tools import odbc_ops


CANNED_MEMORY_HIT = (
    "1 match(es) for 'where are customers connection:demo_db':\n"
    "- [customer_lookup] score=3 "
    "tags=[table:fake_table_a,table:fake_view_b,connection:demo_db,src:memo.md] "
    "intent='顧客テーブルの場所'\n"
    "    ここに顧客情報がある想定のスニペット (fake data only)"
)


def _raise_if_called(*args, **kwargs):
    raise AssertionError("odbc_tables must not be called when memory has hits")


# ===========================================================================
# memory hit -> no live odbc call
# ===========================================================================


def test_memory_hit_extracts_tables_and_skips_odbc(monkeypatch):
    monkeypatch.setattr(pm, "procedural_memory_search", lambda query, limit=10: CANNED_MEMORY_HIT)
    monkeypatch.setattr(odbc_ops, "odbc_tables", _raise_if_called)

    out = dd.find_db_objects("where are customers", connection="demo_db")

    assert "fake_table_a" in out
    assert "fake_view_b" in out
    assert "根拠" in out
    assert "顧客テーブルの場所" in out
    assert "ライブ探索" not in out


def test_extract_tagged_tokens_dedupes_and_preserves_order():
    tokens = dd._extract_tagged_tokens(
        "tags=[table:foo,table:bar,table:foo,connection:demo_db]", "table"
    )
    assert tokens == ["foo", "bar"]


def test_intent_and_snippet_from_block_strips_quotes():
    block = "- [slug] score=1 tags=[] intent='hello world'\n    some snippet text"
    intent, snippet = dd._intent_and_snippet_from_block(block)
    assert intent == "hello world"
    assert snippet == "some snippet text"


# ===========================================================================
# memory empty + connection given -> live odbc fallback, clearly labeled
# ===========================================================================


def test_memory_empty_with_connection_falls_back_to_live_odbc(monkeypatch):
    monkeypatch.setattr(pm, "procedural_memory_search", lambda query, limit=10: "(no matches)")
    canned_live = (
        f"{'catalog':<14}  {'schema':<14}  {'name':<30}  type\n"
        f"{'fakecat':<14}  {'dbo':<14}  {'fake_live_table':<30}  TABLE\n"
        "--- 1 object(s)"
    )
    monkeypatch.setattr(odbc_ops, "odbc_tables", lambda connection, schema=None, catalog=None: canned_live)

    out = dd.find_db_objects("where are orders", connection="demo_db")

    assert "memoryに該当なし" in out
    assert "ライブ探索します" in out
    assert "fake_live_table" in out


def test_memory_empty_with_connection_and_odbc_error_is_not_raised(monkeypatch):
    monkeypatch.setattr(pm, "procedural_memory_search", lambda query, limit=10: "(no matches)")
    monkeypatch.setattr(
        odbc_ops, "odbc_tables",
        lambda connection, schema=None, catalog=None: "[odbc_tables error: RuntimeError: no driver]",
    )

    out = dd.find_db_objects("where are orders", connection="demo_db")

    assert "ライブ探索も失敗" in out
    assert "[find_db_objects error" not in out


# ===========================================================================
# memory empty + no connection -> guidance only, no odbc call
# ===========================================================================


def test_memory_empty_without_connection_gives_guidance_and_skips_odbc(monkeypatch):
    monkeypatch.setattr(pm, "procedural_memory_search", lambda query, limit=10: "(no matches)")
    monkeypatch.setattr(odbc_ops, "odbc_tables", _raise_if_called)

    out = dd.find_db_objects("where are orders")

    assert "procedural_memory_import_markdown" in out
    assert "MCP_DATA_MEMORY_AUTO" in out


# ===========================================================================
# guardrails
# ===========================================================================


def test_empty_question_is_rejected():
    out = dd.find_db_objects("")
    assert out.startswith("[find_db_objects error")


def test_never_raises_on_unexpected_memory_error(monkeypatch):
    def _boom(query, limit=10):
        raise RuntimeError("boom")

    monkeypatch.setattr(pm, "procedural_memory_search", _boom)
    out = dd.find_db_objects("anything")
    assert out.startswith("[find_db_objects error")
