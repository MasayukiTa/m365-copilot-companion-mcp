# -*- coding: utf-8 -*-
"""Two measured gaps of the semantic layer, with synthetic fixtures only (no real table names).

1. A table the glossary (or a metric, or a join) points at but that has no catalogue file used to
   be invisible: catalog_search could not find it, so an agent answered from a look-alike table
   and said the thing did not exist. The store now reports the dangling reference and
   catalog_search / catalog_describe answer from the reference instead of from nothing.
2. Two declared joins between the same pair of tables where one only ADDS conditions to the other
   and is trusted less (measured: the extra condition made the join return no rows) were both
   returned as equal options. The less trusted superset is now demoted, not offered as a join.

Run: pytest -q tools\\test_semantic_gaps.py
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.semantic import catalog, store  # noqa: E402


def _write(root, rel, obj):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def sem(tmp_path):
    _write(tmp_path, "glossary.json", {"terms": [
        {"term": "defect count", "synonyms": ["rejects"], "meaning": "Number of rejected units",
         "resolves_to": {"kind": "column", "table": "V_defects", "column": "defect_qty"}},
        {"term": "revenue", "meaning": "Invoiced amount",
         "resolves_to": {"kind": "column", "table": "orders", "column": "amount"}},
    ]})
    _write(tmp_path, "metrics.json", {"metrics": [
        {"name": "scrap_rate", "sql": "SELECT 1", "tables": ["orders", "scrap_log"]}]})
    _write(tmp_path, "catalog/orders.json", {
        "name": "orders", "kind": "table", "description": "Customer orders",
        "columns": [{"name": "amount", "meaning": "invoiced amount"}],
        "joins": [
            {"to": "customers", "on": "orders.cust_id = customers.id", "confidence": 0.9},
            {"to": "customers",
             "on": "orders.cust_id = customers.id AND orders.region = customers.region",
             "confidence": 0.8},
        ]})
    _write(tmp_path, "catalog/customers.json", {
        "name": "customers", "kind": "table", "description": "Customers",
        "columns": [{"name": "id"}, {"name": "region"}]})
    return store.load_directory(tmp_path)


# ----------------------------------------------------------------------- 1. dangling references

def test_a_referenced_table_without_a_catalogue_file_is_reported(sem):
    assert sorted(v["name"] for v in sem.uncatalogued.values()) == ["V_defects", "scrap_log"]
    warns = [p for p in sem.problems if p.level == "warning" and "no catalogue file" in p.message]
    assert any("V_defects" in p.message for p in warns)
    assert sem.status()["uncatalogued"] == 2


def test_catalog_search_finds_the_table_through_the_glossary_reference(sem):
    r = catalog.catalog_search("defect count", store=sem)
    assert r["ok"] and r["results"], r
    top = r["results"][0]
    assert top["table"] == "V_defects"
    assert top["in_catalogue"] is False
    assert top["referenced_by"] == ["glossary: defect count"]
    assert "odbc_columns" in top["note"]


def test_a_catalogued_table_is_still_marked_as_in_the_catalogue(sem):
    r = catalog.catalog_search("customer orders", store=sem)
    assert r["results"][0]["table"] == "orders"
    assert r["results"][0]["in_catalogue"] is True


def test_describe_of_an_uncatalogued_table_says_why_and_who_points_at_it(sem):
    r = catalog.catalog_describe("V_defects", store=sem)
    assert r["ok"] is False and r["in_catalogue"] is False
    assert r["referenced_by"] == ["glossary: defect count"]
    assert "odbc_columns" in r["error"]


def test_describe_of_an_unknown_table_is_unchanged(sem):
    r = catalog.catalog_describe("nothing_like_it", store=sem)
    assert r["ok"] is False and "in_catalogue" not in r


# --------------------------------------------------------------------- 2. demoted superset joins

def test_the_less_trusted_join_that_only_adds_conditions_is_not_offered(sem):
    r = catalog.catalog_joins("orders", "customers", store=sem)
    assert [d["on"] for d in r["direct"]] == ["orders.cust_id = customers.id"]
    assert len(r["demoted"]) == 1
    d = r["demoted"][0]
    assert "orders.region = customers.region" in d["on"]
    assert "adds" in d["reason"] and "0.9" in d["reason"]


def test_paths_do_not_use_the_demoted_join(sem):
    r = catalog.catalog_joins("orders", "customers", store=sem)
    for p in r["paths"]:
        for s in p["steps"]:
            assert "region" not in s["on"]


def test_side_order_and_case_do_not_hide_the_superset(tmp_path):
    _write(tmp_path, "catalog/a.json", {"name": "a", "joins": [
        {"to": "b", "on": "a.k = b.k", "confidence": 0.9}]})
    _write(tmp_path, "catalog/b.json", {"name": "b", "joins": [
        {"to": "a", "on": "B.K = A.K and b.x = a.x", "confidence": 0.8}]})
    r = catalog.catalog_joins("a", "b", store=store.load_directory(tmp_path))
    assert len(r["direct"]) == 1 and len(r["demoted"]) == 1


def test_a_more_trusted_superset_is_kept(tmp_path):
    _write(tmp_path, "catalog/a.json", {"name": "a", "joins": [
        {"to": "b", "on": "a.k = b.k", "confidence": 0.7},
        {"to": "b", "on": "a.k = b.k AND a.x = b.x", "confidence": 0.9}]})
    _write(tmp_path, "catalog/b.json", {"name": "b"})
    r = catalog.catalog_joins("a", "b", store=store.load_directory(tmp_path))
    assert len(r["direct"]) == 2 and r["demoted"] == []


def test_unrelated_joins_between_the_same_tables_are_both_kept(tmp_path):
    _write(tmp_path, "catalog/a.json", {"name": "a", "joins": [
        {"to": "b", "on": "a.k = b.k", "confidence": 0.9},
        {"to": "b", "on": "a.m = b.m", "confidence": 0.8}]})
    _write(tmp_path, "catalog/b.json", {"name": "b"})
    r = catalog.catalog_joins("a", "b", store=store.load_directory(tmp_path))
    assert len(r["direct"]) == 2 and r["demoted"] == []


# ------------------------------------------------- 3. words that only name the kind of thing asked

def test_structure_words_in_the_question_do_not_outweigh_the_subject(tmp_path):
    """'テーブル' and '列' say what KIND of answer is wanted, not what it is about. Tables whose text
    merely mentions them used to outrank the table that matches the subject."""
    _write(tmp_path, "catalog/decoy.json", {
        "name": "decoy", "description": "注文の控え",
        "pitfalls": ["このテーブルの列は一致確認済み。"],
        "columns": [{"name": "c1", "meaning": "列の説明"}]})
    _write(tmp_path, "catalog/target.json", {
        "name": "target", "description": "不良の数量を持つ",
        "columns": [{"name": "qty", "meaning": "不良の枚数"}]})
    st = store.load_directory(tmp_path)
    r = catalog.catalog_search("不良が入っているテーブルと列はどれですか", store=st)
    assert r["results"][0]["table"] == "target"


def test_a_question_made_only_of_structure_words_still_searches(tmp_path):
    _write(tmp_path, "catalog/decoy.json", {"name": "decoy", "description": "テーブルの列"})
    st = store.load_directory(tmp_path)
    assert catalog.catalog_search("テーブル", store=st)["results"]
