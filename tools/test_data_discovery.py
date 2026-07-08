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
from tools import data_aliases


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


# ===========================================================================
# extract_keywords -- unsegmented Japanese question fix (defect A)
# ===========================================================================


def test_extract_keywords_unsegmented_japanese_yields_multiple_keywords():
    kws = dd.extract_keywords("PAPの材料トレースで見るべきテーブルは？")
    assert len(kws) >= 2
    assert "PAP" in kws
    # at least one kanji/katakana content run must have been carved out
    assert any(("材料" in k or "トレース" in k or "テーブル" in k) for k in kws)


def test_extract_keywords_spaced_query_unchanged():
    kws = dd.extract_keywords("PAP 材料トレース")
    assert kws == ["PAP", "材料トレース"]


def test_extract_keywords_empty_or_blank_returns_empty_list():
    assert dd.extract_keywords("") == []
    assert dd.extract_keywords("   ") == []


def test_extract_keywords_never_raises_on_non_string():
    assert dd.extract_keywords(None) == []
    assert dd.extract_keywords(123) == []


def test_find_db_objects_uses_extracted_keywords_for_unsegmented_question(monkeypatch):
    """The live-verified defect: an unsegmented Japanese question must still reach
    procedural_memory_search with usable (space-joined) keywords, not the raw blob."""
    captured = {}

    def _fake_search(query, limit=10):
        captured["query"] = query
        return CANNED_MEMORY_HIT

    monkeypatch.setattr(pm, "procedural_memory_search", _fake_search)
    monkeypatch.setattr(odbc_ops, "odbc_tables", _raise_if_called)

    dd.find_db_objects("PAPの材料トレースで見るべきテーブルは？")

    assert "PAP" in captured["query"]
    assert " " in captured["query"]  # segmented into multiple space-joined tokens
    assert captured["query"] != "PAPの材料トレースで見るべきテーブルは？"


# ===========================================================================
# SQL-stopword display filter (defect B, display-side)
# ===========================================================================


def test_sql_stopword_tags_are_filtered_from_candidates(monkeypatch):
    noisy_hit = (
        "1 match(es) for 'x':\n"
        "- [noisy] score=2 "
        "tags=[table:group,table:having,table:fake_table_a,table:dateadd] "
        "intent='noisy import'\n"
        "    body"
    )
    monkeypatch.setattr(pm, "procedural_memory_search", lambda query, limit=10: noisy_hit)

    out = dd.find_db_objects("anything", connection="demo_db")

    assert "  - fake_table_a" in out
    assert "  - group" not in out
    assert "  - having" not in out
    assert "  - dateadd" not in out


# ===========================================================================
# alias expansion wiring (tools/data_aliases.py plugged into find_db_objects)
# ===========================================================================


def test_alias_expansion_widens_query_and_notes_it_in_output(monkeypatch):
    monkeypatch.setattr(
        data_aliases, "expand_terms",
        lambda terms: list(terms) + ["synonym_term"],
    )
    captured = {}

    def _fake_search(query, limit=10):
        captured["query"] = query
        return CANNED_MEMORY_HIT

    monkeypatch.setattr(pm, "procedural_memory_search", _fake_search)
    monkeypatch.setattr(odbc_ops, "odbc_tables", _raise_if_called)

    out = dd.find_db_objects("customers", connection="demo_db")

    assert "synonym_term" in captured["query"]
    assert "同義語展開: 1語" in out


def test_alias_expansion_absent_adds_no_note(monkeypatch):
    monkeypatch.setattr(data_aliases, "expand_terms", lambda terms: list(terms))
    monkeypatch.setattr(pm, "procedural_memory_search", lambda query, limit=10: CANNED_MEMORY_HIT)
    monkeypatch.setattr(odbc_ops, "odbc_tables", _raise_if_called)

    out = dd.find_db_objects("customers", connection="demo_db")

    assert "同義語展開" not in out


def test_alias_expansion_is_capped(monkeypatch):
    many_synonyms = [f"syn{i}" for i in range(50)]
    monkeypatch.setattr(
        data_aliases, "expand_terms",
        lambda terms: list(terms) + many_synonyms,
    )
    captured = {}

    def _fake_search(query, limit=10):
        captured["query"] = query
        return CANNED_MEMORY_HIT

    monkeypatch.setattr(pm, "procedural_memory_search", _fake_search)
    monkeypatch.setattr(odbc_ops, "odbc_tables", _raise_if_called)

    dd.find_db_objects("customers", connection="demo_db")

    # capped at 16 total keywords -- query token count stays bounded
    assert len(captured["query"].split()) <= 17  # +1 for "connection:demo_db"


def test_alias_expansion_failure_falls_back_to_unexpanded_keywords(monkeypatch):
    def _boom(terms):
        raise RuntimeError("alias store exploded")

    monkeypatch.setattr(data_aliases, "expand_terms", _boom)
    captured = {}

    def _fake_search(query, limit=10):
        captured["query"] = query
        return CANNED_MEMORY_HIT

    monkeypatch.setattr(pm, "procedural_memory_search", _fake_search)
    monkeypatch.setattr(odbc_ops, "odbc_tables", _raise_if_called)

    out = dd.find_db_objects("where are customers", connection="demo_db")

    # still works: search still ran with the unexpanded query, no crash
    assert "fake_table_a" in out
    assert "customers" in captured["query"] or "顧客" in captured["query"] or captured["query"]
    assert "同義語展開" not in out


def test_alias_expansion_not_attempted_when_no_keywords_extracted(monkeypatch):
    """extract_keywords("") never gets here (rejected earlier), but a question
    that extracts to [] should skip aliasing rather than expand []."""
    called = {"n": 0}

    def _track(terms):
        called["n"] += 1
        return terms

    monkeypatch.setattr(data_aliases, "expand_terms", _track)
    monkeypatch.setattr(pm, "procedural_memory_search", lambda query, limit=10: "(no matches)")
    monkeypatch.setattr(odbc_ops, "odbc_tables", _raise_if_called)

    dd.find_db_objects("...")  # punctuation-only -> extract_keywords yields []

    assert called["n"] == 0
