# -*- coding: utf-8 -*-
"""Semantic layer: store, resolve, metric_sql, catalog. Synthetic general-word fixtures only."""
import json
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.semantic import catalog, dates, metric_sql as ms, resolve, store  # noqa: E402

TODAY = date(2026, 10, 14)        # a Wednesday


def _w(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


GLOSSARY = {"terms": [
    {"term": "sales", "synonyms": ["revenue", "売上", "売り上げ"],
     "meaning": "Invoiced amount", "status": "certified",
     "resolves_to": {"kind": "column", "table": "orders", "column": "amount"},
     "caveats": ["excludes tax"], "owner_note": "unknown key kept"},
    {"term": "region", "synonyms": ["area", "地域"], "meaning": "Sales region",
     "resolves_to": {"kind": "column", "table": "customers", "column": "region"}},
    {"term": "返品", "synonyms": ["リターン"], "meaning": "Returned order",
     "resolves_to": {"kind": "filter", "table": "orders", "expr": "status = 'RETURNED'"},
     "status": "draft"},
    {"term": "返品商品", "synonyms": [], "meaning": "A returned product", "status": "draft"},
    {"term": "channel", "synonyms": ["area"], "meaning": "Sales channel"},
]}

METRICS = {"metrics": [
    {"name": "total_sales", "description": "Sum of sales per region",
     "sql": "SELECT region, SUM(amount) AS total FROM orders "
            "WHERE order_date >= :start AND order_date < :end AND note <> 'a:start' "
            "GROUP BY region",
     "params": [{"name": "start", "type": "date", "default": None},
                {"name": "end", "type": "date", "default": None}],
     "dimensions": ["region"], "tables": ["orders"], "synonyms": ["sales total", "売上合計"],
     "status": "certified", "caveats": ["tax excluded"]},
    {"name": "top_regions", "description": "Top N regions",
     "sql": "WITH t AS (SELECT region, SUM(amount) a FROM orders WHERE region = :region "
            "OR region = :region) SELECT TOP (:n) * FROM t",
     "params": [{"name": "region", "type": "string"},
                {"name": "n", "type": "int", "default": 5}]},
    {"name": "wipe", "sql": "DELETE FROM orders WHERE order_date < :start",
     "params": [{"name": "start", "type": "date"}]},
    {"name": "two", "sql": "SELECT 1; SELECT 2"},
    {"name": "sneaky", "sql": "SELECT * INTO copy FROM orders"},
    {"name": "undeclared", "sql": "SELECT :x", "params": []},
]}


def _table(name, desc, cols, joins=(), **kw):
    t = {"name": name, "kind": "table", "description": desc, "columns": cols,
         "joins": list(joins)}
    t.update(kw)
    return t


CATALOG = [
    _table("orders", "Customer orders, one row per order",
           [{"name": "order_id", "type": "int", "meaning": "order key"},
            {"name": "amount", "type": "decimal", "meaning": "invoiced amount excluding tax"},
            {"name": "status", "type": "char", "meaning": "order state",
             "values": {"RETURNED": "goods came back", "OPEN": "not shipped"},
             "notes": "values are upper case"}],
           [{"to": "customers", "on": "orders.customer_id = customers.id", "confidence": 0.95},
            {"to": "shipments", "on": "orders.order_id = shipments.order_id",
             "confidence": 0.8}],
           pitfalls=["order_id is reused every January"], performance="index on order_date",
           row_estimate=1200000, keys=["order_id"], status="certified"),
    _table("customers", "顧客マスタ。顧客ごとに1行", [
        {"name": "id", "type": "int", "meaning": "顧客番号"},
        {"name": "region", "type": "varchar", "meaning": "地域コード"}],
        [{"to": "regions", "on": "customers.region = regions.code", "confidence": 0.9}]),
    _table("regions", "Region master", [{"name": "code", "meaning": "region code"}]),
    _table("shipments", "Shipments leaving the warehouse", [
        {"name": "order_id", "meaning": "order key"}],
        [{"to": "warehouses", "on": "shipments.wh = warehouses.id"}]),
    _table("warehouses", "Warehouse master", [{"name": "id", "meaning": "warehouse id"}]),
    _table("audit_notes", "Free text notes", [{"name": "note", "meaning": "text"}]),
]


@pytest.fixture()
def sem(tmp_path, monkeypatch):
    d = tmp_path / "sem"
    _w(d / "glossary.json", GLOSSARY)
    _w(d / "metrics.json", METRICS)
    for t in CATALOG:
        _w(d / "catalog" / (t["name"] + ".json"), t)
    monkeypatch.setenv(store.ENV_VAR, str(d))
    store._CACHE.clear()
    yield d
    store._CACHE.clear()


# ------------------------------------------------------------------------------- store

def test_unset_is_reported_not_treated_as_empty(monkeypatch):
    monkeypatch.delenv(store.ENV_VAR, raising=False)
    store._CACHE.clear()
    r = resolve.resolve("sales")
    assert r["ok"] is False and "MCP_SEMANTIC_DIR" in r["error"]
    assert r["undefined"] is None            # not "everything is undefined"
    assert catalog.catalog_search("orders")["ok"] is False
    assert ms.metric_sql("total_sales")["ok"] is False
    assert catalog.catalog_describe("orders")["ok"] is False
    assert catalog.catalog_joins("a", "b")["ok"] is False


def test_folder_that_does_not_exist(tmp_path, monkeypatch):
    monkeypatch.setenv(store.ENV_VAR, str(tmp_path / "nope"))
    store._CACHE.clear()
    assert "not a folder" in resolve.resolve("x")["error"]


def test_loads_everything_and_keeps_unknown_keys(sem):
    s = store.get_store()
    assert len(s.terms) == 5 and len(s.metrics) == 6 and len(s.tables) == 6
    assert s.terms[0]["owner_note"] == "unknown key kept"
    assert s.problems == [] or all(p.level == "warning" for p in s.problems)


def test_empty_folder_is_configured_but_empty(tmp_path, monkeypatch):
    monkeypatch.setenv(store.ENV_VAR, str(tmp_path))
    store._CACHE.clear()
    s = store.get_store()
    assert s.configured and s.exists and not s.terms and not s.metrics and not s.tables
    r = resolve.resolve("sales")
    assert r["ok"] is True and r["matches"] == [] and r["undefined"][0]["word"] == "sales"


def test_broken_json_names_file_and_place_and_others_still_load(sem):
    (sem / "glossary.json").write_text('{"terms": [\n {"term": "a",,}\n]}', encoding="utf-8")
    (sem / "catalog" / "regions.json").write_text("{bad", encoding="utf-8")
    s = store.get_store()
    msgs = {(p.file, p.where) for p in s.problems if p.level == "error"}
    assert any(f == "glossary.json" and "line 2" in w for f, w in msgs)
    assert any(f == "catalog/regions.json" for f, _ in msgs)
    assert not s.terms                       # that file gave nothing ...
    assert len(s.metrics) == 6               # ... but the rest still loaded
    assert s.find_table("orders") is not None and s.find_table("regions") is None


def test_bad_entries_are_skipped_with_location(sem):
    _w(sem / "glossary.json", {"terms": [
        {"synonyms": ["x"]}, "oops", {"term": "ok", "synonyms": 5, "status": "weird",
                                      "resolves_to": "bad"}]})
    s = store.get_store()
    assert [t["term"] for t in s.terms] == ["ok"]
    assert s.terms[0]["status"] == "draft" and s.terms[0]["synonyms"] == []
    wheres = [p.where for p in s.problems if p.file == "glossary.json"]
    assert "terms[0]" in wheres and "terms[1]" in wheres
    assert "terms[2].synonyms" in wheres and "terms[2].resolves_to" in wheres


def test_wrong_top_level_shape_is_reported(sem):
    _w(sem / "metrics.json", [1, 2])
    s = store.get_store()
    assert any(p.file == "metrics.json" and p.level == "error" for p in s.problems)


def test_non_utf8_file_is_reported(sem):
    (sem / "glossary.json").write_bytes('{"terms": []}'.encode("cp932") + b"\x82")
    s = store.get_store()
    assert any("UTF-8" in p.message for p in s.problems)


def test_utf8_bom_is_tolerated(sem):
    (sem / "glossary.json").write_bytes(
        b"\xef\xbb\xbf" + json.dumps({"terms": [{"term": "bom"}]}).encode("utf-8"))
    assert [t["term"] for t in store.get_store().terms] == ["bom"]


def test_reload_when_a_file_changes(sem):
    assert len(store.get_store().terms) == 5
    assert store.get_store() is store.get_store()          # cached
    g = {"terms": GLOSSARY["terms"] + [{"term": "latency"}]}
    _w(sem / "glossary.json", g)
    st = (sem / "glossary.json").stat()
    os.utime(sem / "glossary.json", ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert len(store.get_store().terms) == 6


def test_new_catalog_file_is_picked_up(sem):
    assert store.get_store().find_table("extra") is None
    _w(sem / "catalog" / "extra.json", _table("extra", "x", []))
    assert store.get_store().find_table("extra") is not None


# ----------------------------------------------------------------------------- resolve

def names(r):
    return [m["name"] for m in r["matches"]]


def test_exact_and_synonym_and_japanese(sem):
    assert "sales" in names(resolve.resolve("show revenue by month"))
    assert "sales" in names(resolve.resolve("売上を見たい"))
    m = [m for m in resolve.resolve("revenue")["matches"] if m["name"] == "sales"][0]
    assert m["resolves_to"]["column"] == "amount" and m["caveats"] == ["excludes tax"]
    assert m["status"] == "certified" and m["match"] == "exact" and m["matched_text"] == "revenue"


def test_full_width_and_case_and_plural(sem):
    assert "sales" in names(resolve.resolve("ＳＡＬＥＳ"))
    assert "region" in names(resolve.resolve("by Regions"))
    assert "region" in names(resolve.resolve("地域別"))


def test_substring_in_a_compound_word(sem):
    r = resolve.resolve("返品率を教えて")
    m = [m for m in r["matches"] if m["name"] == "返品"][0]
    assert m["defined"] and m["match"] == "substring"
    assert all(u["word"] != "返品" for u in r["undefined"])


def test_word_missing_from_dictionary_is_undefined_not_guessed(sem):
    r = resolve.resolve("過去1か月の欠陥データを集計して", today="2026-10-14")
    assert [u["word"] for u in r["undefined"]] == ["欠陥"]
    assert r["matches"] == []
    assert r["periods"][0]["start"] == "2026-09-15"
    assert any("undefined" in w for w in r["warnings"])


def test_defined_and_undefined_in_one_request(sem):
    r = resolve.resolve("show revenue and defect count for last month", today="2026-10-14")
    assert "sales" in names(r)
    assert [u["word"] for u in r["undefined"]] == ["defect"]


def test_generic_words_are_not_reported_undefined(sem):
    r = resolve.resolve("データを一覧で表示して、集計して", today="2026-10-14")
    assert r["undefined"] == []


def test_partial_match_is_offered_but_word_stays_undefined(sem):
    r = resolve.resolve("返品商 を教えて")
    # 返品商 is not a defined word; it is similar to 返品商品 only
    near = [m for m in r["matches"] if not m["defined"]]
    assert near and near[0]["name"] == "返品商品"


def test_ranking_exact_beats_partial(sem):
    r = resolve.resolve("返品商品")
    assert names(r)[0] == "返品商品"
    assert r["matches"][0]["score"] >= r["matches"][1]["score"]


def test_ranking_certified_wins_a_tie(sem):
    _w(sem / "glossary.json", {"terms": [
        {"term": "alpha", "synonyms": ["thing"], "status": "draft"},
        {"term": "beta", "synonyms": ["thing"], "status": "certified"}]})
    r = resolve.resolve("thing")
    assert names(r) == ["beta", "alpha"]
    assert r["ambiguous"] and set(r["ambiguous"][0]["claimed_by"]) == {"term:alpha", "term:beta"}


def test_shared_synonym_is_reported_ambiguous(sem):
    r = resolve.resolve("area")
    assert {"region", "channel"} <= set(names(r))
    assert r["ambiguous"]


def test_metric_is_found_by_synonym(sem):
    r = resolve.resolve("売上合計を出して")
    mm = [m for m in r["matches"] if m["type"] == "metric"]
    assert mm and mm[0]["name"] == "total_sales" and mm[0]["params"] == ["start", "end"]


def test_draft_is_flagged(sem):
    r = resolve.resolve("返品")
    assert any("draft" in w for w in r["warnings"])


def test_store_problems_are_surfaced(sem):
    (sem / "metrics.json").write_text("{", encoding="utf-8")
    r = resolve.resolve("sales")
    assert r["store_problems"] and any("could not be loaded" in w for w in r["warnings"])


# --------------------------------------------------------------------------------- dates

@pytest.mark.parametrize("phrase,start,end", [
    ("過去1か月", "2026-09-15", "2026-10-15"),
    ("過去１か月", "2026-09-15", "2026-10-15"),
    ("past 1 month", "2026-09-15", "2026-10-15"),
    ("先月", "2026-09-01", "2026-10-01"),
    ("last month", "2026-09-01", "2026-10-01"),
    ("今月", "2026-10-01", "2026-11-01"),
    ("this month", "2026-10-01", "2026-11-01"),
    ("直近7日", "2026-10-08", "2026-10-15"),
    ("last 7 days", "2026-10-08", "2026-10-15"),
    ("過去3か月", "2026-07-15", "2026-10-15"),
    ("過去一年", "2025-10-15", "2026-10-15"),
    ("昨日", "2026-10-13", "2026-10-14"),
    ("今日", "2026-10-14", "2026-10-15"),
    ("今週", "2026-10-12", "2026-10-19"),
    ("先週", "2026-10-05", "2026-10-12"),
    ("昨年", "2025-01-01", "2026-01-01"),
    ("先々月", "2026-08-01", "2026-09-01"),
    ("過去半年", "2026-04-15", "2026-10-15"),
])
def test_relative_dates(phrase, start, end):
    p = dates.resolve_period(phrase, TODAY)
    assert p is not None, phrase
    assert (p.start.isoformat(), p.end.isoformat()) == (start, end)


def test_month_end_is_clamped():
    p = dates.resolve_period("過去1か月", date(2026, 3, 31))
    assert (p.start, p.end) == (date(2026, 3, 1), date(2026, 4, 1))
    p = dates.resolve_period("先月", date(2026, 1, 15))
    assert (p.start, p.end) == (date(2025, 12, 1), date(2026, 1, 1))


def test_not_a_period():
    assert dates.resolve_period("defect rate", TODAY) is None
    assert dates.resolve_period("先月の売上", TODAY) is None     # more than a date phrase


# ------------------------------------------------------------------------------ metric_sql

def test_binds_are_question_marks_and_values_never_in_sql(sem):
    r = ms.metric_sql("total_sales", {"start": "2026-01-01", "end": "2026-02-01"})
    assert r["ok"] is True
    assert r["sql"].count("?") == 2 and ":start" in r["sql"]      # the one inside a string stays
    assert "'a:start'" in r["sql"]
    assert r["bind"] == ["2026-01-01", "2026-02-01"] and r["bind_names"] == ["start", "end"]
    assert "2026-01-01" not in r["sql"] and r["executed"] is False


def test_hostile_value_stays_a_bind_value(sem):
    evil = "x'); DROP TABLE orders; --"
    r = ms.metric_sql("top_regions", {"region": evil})
    assert r["ok"] and evil not in r["sql"] and evil in r["bind"]
    assert r["bind"] == [evil, evil, 5] and r["sql"].count("?") == 3     # repeated, + default
    assert any("default" in n for n in r["notes"])


def test_period_fills_start_end_and_reports_it(sem):
    r = ms.metric_sql("売上合計", period="過去1か月", today="2026-10-14")
    assert r["ok"] and r["bind"] == ["2026-09-15", "2026-10-15"]
    rd = r["resolved_dates"][0]
    assert rd["base_date"] == "2026-10-14" and "exclusive" in rd["convention"]
    assert rd["filled"] == {"start": "2026-09-15", "end": "2026-10-15"}
    for ph, b in [("last month", ["2026-09-01", "2026-10-01"]),
                  ("this month", ["2026-10-01", "2026-11-01"]),
                  ("直近7日", ["2026-10-08", "2026-10-15"])]:
        assert ms.metric_sql("total_sales", period=ph, today="2026-10-14")["bind"] == b


def test_explicit_value_beats_period(sem):
    r = ms.metric_sql("total_sales", {"start": "2026-01-01"}, period="先月", today="2026-10-14")
    assert r["bind"] == ["2026-01-01", "2026-10-01"]


def test_unreadable_period_is_an_error(sem):
    r = ms.metric_sql("total_sales", period="whenever", today="2026-10-14")
    assert r["ok"] is False and "relative date" in r["error"]


def test_missing_parameters_are_listed(sem):
    r = ms.metric_sql("total_sales", {"start": "2026-01-01"})
    assert r["ok"] is False and r["missing"] == ["end"]
    r = ms.metric_sql("top_regions")
    assert r["missing"] == ["region"]


def test_unknown_and_badly_typed_parameters(sem):
    assert "unknown parameter" in ms.metric_sql("total_sales", {"bogus": 1})["error"]
    assert "date" in ms.metric_sql("total_sales", {"start": "yesterday", "end": "x"})["error"]
    assert "integer" in ms.metric_sql("top_regions", {"region": "a", "n": "many"})["error"]
    assert ms.metric_sql("top_regions", {"region": "a", "n": "7"})["bind"][-1] == 7


def test_only_select_and_with_are_accepted(sem):
    for bad in ("wipe", "two", "sneaky"):
        r = ms.metric_sql(bad, {"start": "2026-01-01"})
        assert r["ok"] is False and "refused" in r["error"], bad
    assert ms.metric_sql("top_regions", {"region": "a"})["ok"]            # WITH is fine


@pytest.mark.parametrize("sql,ok", [
    ("select 1", True), ("  -- note\n SELECT 1;", True), ("(select 1)", True),
    ("WITH a AS (SELECT 1) SELECT * FROM a", True),
    ("SELECT 'drop table x; delete' AS s", True),            # inside a string literal
    ("SELECT [update] FROM t", True),                          # quoted identifier
    ("select 1 /* ; delete */", True),
    ("DELETE FROM t", False), ("UPDATE t SET a=1", False), ("EXEC sp_who", False),
    ("SELECT 1; DROP TABLE t", False), ("INSERT INTO t SELECT 1", False),
    ("SELECT * INTO x FROM t", False), ("", False), ("MERGE t USING s ON 1=1", False),
    ("SELECT 1 WHERE 1=1; EXEC xp_cmdshell 'dir'", False),
])
def test_read_only_check(sql, ok):
    assert (ms.check_read_only(sql) is None) is ok


def test_undeclared_sql_parameter_is_an_error(sem):
    assert "does not declare" in ms.metric_sql("undeclared")["error"]


def test_unknown_metric_lists_known_ones(sem):
    r = ms.metric_sql("nope")
    assert r["ok"] is False and "total_sales" in r["known_metrics"]


def test_bind_named_ignores_casts_comments_and_quotes():
    sql, names = ms.bind_named(
        "SELECT a::int, ':x', \":y\", [:z] FROM t -- :c\n WHERE b = :b /* :d */ AND c = :b")
    assert names == ["b", "b"] and sql.count("?") == 2


def test_draft_metric_is_flagged(sem):
    r = ms.metric_sql("top_regions", {"region": "a"})
    assert any("draft" in n for n in r["notes"])


# ------------------------------------------------------------------------------ catalog

def test_search_ranks_name_hit_above_mention(sem):
    r = catalog.catalog_search("orders")
    assert r["results"][0]["table"] == "orders"


def test_search_by_column_value_meaning_and_pitfall(sem):
    assert catalog.catalog_search("goods came back")["results"][0]["table"] == "orders"
    assert catalog.catalog_search("reused every January")["results"][0]["table"] == "orders"
    top = catalog.catalog_search("RETURNED")["results"][0]
    assert top["table"] == "orders" and any(h.get("column") == "status" for h in top["matched"])


def test_search_in_japanese(sem):
    r = catalog.catalog_search("顧客の地域")
    assert r["results"][0]["table"] == "customers"
    assert catalog.catalog_search("顧客番号")["results"][0]["table"] == "customers"
    assert catalog.catalog_search("ＯＲＤＥＲＳ")["results"][0]["table"] == "orders"


def test_search_scores_and_limit_and_no_hit(sem):
    r = catalog.catalog_search("master", limit=1)
    assert len(r["results"]) == 1 and r["count"] >= 2 and r["results"][0]["score"] > 0
    assert catalog.catalog_search("zzzqqq")["results"] == []
    assert catalog.catalog_search("   ")["ok"] is False


def test_describe_has_joins_both_ways_pitfalls_and_performance(sem):
    d = catalog.catalog_describe("orders")
    assert d["ok"] and d["pitfalls"] and d["performance"] == "index on order_date"
    assert d["row_estimate"] == 1200000 and d["keys"] == ["order_id"]
    assert {j["to"] for j in d["joins"]} == {"customers", "shipments"}
    r = catalog.catalog_describe("Customers")                 # case-insensitive
    assert {j["to"] for j in r["joins"]} == {"regions", "orders"}
    incoming = [j for j in r["joins"] if j["to"] == "orders"][0]
    assert incoming["direction"] == "declared by orders"


def test_describe_unknown_suggests(sem):
    d = catalog.catalog_describe("order")
    assert d["ok"] is False and "orders" in d["did_you_mean"]


def test_joins_direct_and_multi_hop(sem):
    r = catalog.catalog_joins("orders", "customers")
    assert r["direct"][0]["on"] == "orders.customer_id = customers.id"
    assert r["paths"][0]["tables"] == ["orders", "customers"]
    r = catalog.catalog_joins("orders", "regions")
    assert r["direct"] == [] and r["paths"][0]["tables"] == ["orders", "customers", "regions"]
    assert r["paths"][0]["hops"] == 2 and r["paths"][0]["confidence"] == round(0.95 * 0.9, 4)
    r = catalog.catalog_joins("regions", "warehouses")        # four hops, found in reverse
    assert r["paths"] == [] and r["notes"]
    r = catalog.catalog_joins("regions", "warehouses", max_hops=4)
    assert r["paths"][0]["tables"] == ["regions", "customers", "orders", "shipments",
                                       "warehouses"]
    assert r["paths"][0]["unrated_hops"] == 1


def test_joins_shortest_first_and_unreachable(sem):
    t = dict(CATALOG[0])
    t["joins"] = t["joins"] + [{"to": "regions", "on": "orders.r = regions.code",
                                "confidence": 0.5}]
    _w(sem / "catalog" / "orders.json", t)
    r = catalog.catalog_joins("orders", "regions")
    assert [p["hops"] for p in r["paths"]] == [1, 2]
    r = catalog.catalog_joins("orders", "audit_notes")
    assert r["paths"] == [] and r["direct"] == [] and "may be missing" in r["notes"][0]


def test_joins_unknown_table(sem):
    r = catalog.catalog_joins("orders", "ordrs")
    assert r["ok"] is False and r["did_you_mean"]["ordrs"] == ["orders"]
    assert catalog.catalog_joins("orders", "orders")["ok"] is False


def test_join_to_a_table_missing_from_the_catalogue_is_a_node(sem):
    r = catalog.catalog_joins("warehouses", "shipments")
    assert r["direct"] and r["direct"][0]["to"] == "shipments"
