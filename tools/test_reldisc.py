"""Relationship discovery (slice R1) against a synthetic SQLite database.

Everything here is generated in the test; nothing refers to a real system.
"""

from __future__ import annotations

import json
import random
import sqlite3

import pytest

from tools.reldisc.candidates import generate_candidates, name_tokens
from tools.reldisc.profile import ProfileConfig, collapse_shape, profile_source, shape_of
from tools.reldisc.run import discover, format_joins, main
from tools.reldisc.source import (
    SqliteSource, open_source, quote_ident, sql_rowcount_mssql, sql_sample_mssql, sql_select_in,
)
from tools.reldisc.store import DiscoveryStore
from tools.reldisc.verify import PairBudget, verify_pair

ALIASES = {"lot": ["ロット"]}
CFG = ProfileConfig(sample_rows=2000, seed=7)


def _fw(s: str) -> str:
    """ASCII digits/letters to their full-width forms."""
    return "".join(chr(ord(c) + 0xFEE0) if c.isalnum() and c.isascii() else c for c in s)


def _build(path, big_rows: int = 0) -> None:
    rnd = random.Random(1234)
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE orders (order_id INTEGER PRIMARY KEY, customer_id INTEGER, note TEXT);
        CREATE TABLE products (sku TEXT, title TEXT);
        CREATE TABLE order_items (item_id INTEGER PRIMARY KEY, sku TEXT, qty INTEGER);
        CREATE TABLE lots (lot_no TEXT, made_on TEXT);
        CREATE TABLE inspections (insp_id INTEGER PRIMARY KEY, "ロットNO" TEXT, result TEXT);
        CREATE TABLE batches (batch_id TEXT, size INTEGER);
        CREATE TABLE batch_results (res_id INTEGER PRIMARY KEY, batch_id TEXT);
        CREATE TABLE fw_codes (code TEXT);
        CREATE TABLE hw_codes (code TEXT);
        CREATE TABLE up_codes (code TEXT);
        CREATE TABLE low_codes (code TEXT);
        CREATE TABLE sensors (sensor_id INTEGER PRIMARY KEY, reading INTEGER);
        CREATE TABLE stock (stock_id INTEGER PRIMARY KEY, units INTEGER);
        CREATE TABLE memos (memo_id INTEGER PRIMARY KEY, ref TEXT);
        """
    )
    con.executemany("INSERT INTO customers VALUES (?,?)", [(i, f"c{i}") for i in range(1, 3001)])
    con.executemany("INSERT INTO orders VALUES (?,?,?)",
                    [(i, rnd.randint(1, 3000), "x") for i in range(1, 6001)])
    skus = [f"P{i:05d}" for i in range(1, 801)]
    con.executemany("INSERT INTO products VALUES (?,?)", [(s, "t" + s) for s in skus])
    con.executemany("INSERT INTO order_items VALUES (?,?,?)",
                    [(i, rnd.choice(skus), rnd.randint(1, 9)) for i in range(1, 4001)])
    lots = [f"L2024-{i:04d}" for i in range(1, 501)]
    con.executemany("INSERT INTO lots VALUES (?,?)", [(l, "2024-01-01") for l in lots])
    con.executemany("INSERT INTO inspections VALUES (?,?,?)",
                    [(i, rnd.choice(lots), "ok") for i in range(1, 1501)])
    batches = [f"B{i:04d}" for i in range(1, 401)]
    con.executemany("INSERT INTO batches VALUES (?,?)", [(b, 10) for b in batches])
    con.executemany("INSERT INTO batch_results VALUES (?,?)",
                    [(i, rnd.choice(batches) + "-01") for i in range(1, 1201)])
    codes = [f"{i:05d}" for i in range(1, 301)]
    con.executemany("INSERT INTO fw_codes VALUES (?)", [(_fw(c),) for c in codes])
    con.executemany("INSERT INTO hw_codes VALUES (?)", [(c,) for c in codes])
    con.executemany("INSERT INTO up_codes VALUES (?)", [(f"ABC-{i:03d}",) for i in range(1, 301)])
    con.executemany("INSERT INTO low_codes VALUES (?)", [(f"abc-{i:03d}",) for i in range(1, 301)])
    con.executemany("INSERT INTO sensors VALUES (?,?)", [(i, rnd.randint(0, 100)) for i in range(1, 1001)])
    con.executemany("INSERT INTO stock VALUES (?,?)", [(i, rnd.randint(0, 100)) for i in range(1, 1001)])
    con.executemany("INSERT INTO memos VALUES (?,?)",
                    [(i, "Q" + str(rnd.randint(10000, 99999))) for i in range(1, 801)])
    if big_rows:
        con.execute("CREATE TABLE events (event_id INTEGER PRIMARY KEY, sku TEXT, payload INTEGER)")
        con.executemany("INSERT INTO events VALUES (?,?,?)",
                        ((i, skus[i % len(skus)], i % 97) for i in range(1, big_rows + 1)))
    con.commit()
    con.close()


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    p = tmp_path_factory.mktemp("reldisc") / "synthetic.sqlite"
    _build(str(p))
    return str(p)


def _pair(joins, t1, c1, t2, c2):
    for j in joins:
        if {(j["a_table"], j["a_column"]), (j["b_table"], j["b_column"])} == {(t1, c1), (t2, c2)}:
            return j
    return None


def _run(db, store_path, **kw):
    src = SqliteSource(db)
    st = DiscoveryStore(str(store_path))
    try:
        rep = discover(src, st, profile_cfg=CFG, aliases=kw.pop("aliases", ALIASES), top=500, **kw)
    finally:
        src.close()
        st.close()
    return rep


@pytest.fixture(scope="module")
def report(db, tmp_path_factory):
    return _run(db, tmp_path_factory.mktemp("store") / "s.sqlite")


# ------------------------------------------------------------------ planted relationships
def test_plain_foreign_keys_are_found(report):
    fk = _pair(report.joins, "order_items", "sku", "products", "sku")
    assert fk and fk["confidence"] >= 0.5 and fk["evidence"]["a_in_b"] == 1.0
    assert (fk["a_table"], fk["b_table"]) == ("order_items", "products")  # key side is B
    assert fk["evidence"]["cardinality"] == "N:1"
    cu = _pair(report.joins, "orders", "customer_id", "customers", "customer_id")
    assert cu and cu["confidence"] >= 0.5 and cu["evidence"]["a_in_b"] == 1.0


def test_differently_named_columns_with_equal_values_are_found(report):
    j = _pair(report.joins, "inspections", "ロットNO", "lots", "lot_no")
    assert j and j["confidence"] >= 0.5
    assert j["evidence"]["a_in_b"] == 1.0 and j["evidence"]["cardinality"] == "N:1"


def test_alias_raises_the_name_score(db):
    src = SqliteSource(db)
    try:
        profs, _ = profile_source(src, CFG)
    finally:
        src.close()

    def name_score(al):
        cs = generate_candidates(profs, al)
        c = [c for c in cs if {c.a_table, c.b_table} == {"inspections", "lots"}]
        return c[0].name_score if c else 0.0

    assert name_score(ALIASES) > name_score(None)


# ------------------------------------------------------------------ known limits of R1
def test_suffix_difference_is_not_a_candidate_and_has_no_raw_containment(db):
    src = SqliteSource(db)
    try:
        profs, _ = profile_source(src, CFG)
        cands = generate_candidates(profs, ALIASES)
        assert not [c for c in cands if {(c.a_table, c.a_column), (c.b_table, c.b_column)}
                    == {("batches", "batch_id"), ("batch_results", "batch_id")}]
        by = {(p.table, p.column): p for p in profs}
        res = verify_pair(src, by[("batch_results", "batch_id")], by[("batches", "batch_id")])
    finally:
        src.close()
    assert res.status == "verified" and res.a_in_b == 0.0 and res.confidence == 0.0


def test_fullwidth_and_case_differences_have_low_raw_containment(db):
    src = SqliteSource(db)
    try:
        profs, _ = profile_source(src, CFG)
        by = {(p.table, p.column): p for p in profs}
        fw = verify_pair(src, by[("fw_codes", "code")], by[("hw_codes", "code")])
        case = verify_pair(src, by[("up_codes", "code")], by[("low_codes", "code")])
        same = verify_pair(src, by[("hw_codes", "code")], by[("hw_codes", "code")])
    finally:
        src.close()
    assert fw.a_in_b == 0.0 and fw.confidence == 0.0
    assert case.a_in_b == 0.0 and case.b_in_a == 0.0 and case.confidence == 0.0
    assert same.a_in_b == 1.0  # the verifier itself is sound: identical values do contain


def test_unrelated_columns_get_no_confident_join(report):
    for t1, c1, t2, c2 in [("sensors", "reading", "stock", "units"),
                           ("memos", "ref", "products", "sku"),
                           ("sensors", "sensor_id", "stock", "stock_id")]:
        j = _pair(report.joins, t1, c1, t2, c2)
        assert j is None or j["confidence"] < 0.2, (t1, c1, t2, c2, j)


def test_every_reported_confident_join_is_a_planted_one(report):
    planted = {
        frozenset({("order_items", "sku"), ("products", "sku")}),
        frozenset({("orders", "customer_id"), ("customers", "customer_id")}),
        frozenset({("inspections", "ロットNO"), ("lots", "lot_no")}),
    }
    for j in report.joins:
        if j["status"] == "verified" and j["confidence"] >= 0.5:
            assert frozenset({(j["a_table"], j["a_column"]), (j["b_table"], j["b_column"])}) in planted, j


# ------------------------------------------------------------------ budgets and sampling
def test_huge_table_is_sampled_not_scanned(tmp_path):
    big = 200_000
    p = str(tmp_path / "big.sqlite")
    _build(p, big_rows=big)
    src = SqliteSource(p)
    st = DiscoveryStore(str(tmp_path / "s.sqlite"))
    try:
        profs, _ = profile_source(src, CFG)
        profile_rows = src.rows_fetched
        rep = discover(src, st, profile_cfg=CFG, aliases=ALIASES, top=500)
        total_rows = src.rows_fetched
    finally:
        src.close()
        st.close()
    ev = [q for q in profs if q.table == "events"]
    assert all(not q.complete and q.sample_n <= CFG.sample_rows for q in ev)
    assert profile_rows < 0.2 * big  # the whole profile of every table, big one included
    assert total_rows < big, total_rows  # profile + ~100 verified pairs together read less than the table once
    j = _pair(rep.joins, "events", "sku", "products", "sku")
    assert j and j["confidence"] >= 0.5
    assert all(x["evidence"].get("rows_read", 0) <= 50_000 for x in rep.joins)
    assert j["evidence"]["rows_read"] < 0.1 * big


def test_per_pair_time_and_row_budgets_mark_the_pair_unverified(db):
    src = SqliteSource(db)
    try:
        profs, _ = profile_source(src, CFG)
        by = {(p.table, p.column): p for p in profs}
        a, b = by[("order_items", "sku")], by[("products", "sku")]
        t = verify_pair(src, a, b, PairBudget(time_s=0.0))
        r = verify_pair(src, a, b, PairBudget(rows=10))
        ok = verify_pair(src, a, b, PairBudget(sample_values=100))
    finally:
        src.close()
    assert (t.status, t.reason) == ("unverified", "budget_time") and t.confidence == 0.0
    assert (r.status, r.reason) == ("unverified", "budget_rows")
    assert ok.status == "verified" and ok.evidence["n_a"] <= 100


def test_run_budget_exhaustion_is_recorded_not_dropped(db, tmp_path):
    rep = _run(db, tmp_path / "s.sqlite", budget_s=0.0)
    assert rep.profile.tables_skipped > 0 and rep.profile.skipped_reasons


# ------------------------------------------------------------------ determinism and store
def _digest(joins):
    return [(j["a_table"], j["a_column"], j["b_table"], j["b_column"], j["confidence"], j["status"],
             j["evidence"].get("a_in_b"), j["evidence"].get("b_in_a"), j["evidence"].get("jaccard"),
             j["evidence"].get("cardinality")) for j in joins]


def test_same_input_gives_same_result(db, tmp_path):
    r1 = _run(db, tmp_path / "a.sqlite")
    r2 = _run(db, tmp_path / "b.sqlite")
    assert _digest(r1.joins) == _digest(r2.joins) and r1.candidates == r2.candidates


def test_rerun_only_redoes_changed_columns(tmp_path):
    p = str(tmp_path / "d.sqlite")
    _build(p)
    store = tmp_path / "s.sqlite"
    first = _run(p, store)
    assert first.profile.columns_reused == 0 and first.profile.columns_profiled > 0

    again = _run(p, store)
    assert again.profile.columns_profiled == 0
    assert again.profile.columns_reused == first.profile.columns_profiled
    assert again.verified == 0 and again.reused == first.verified

    con = sqlite3.connect(p)
    con.execute("INSERT INTO lots VALUES ('L2024-9999', '2024-02-01')")
    con.commit()
    con.close()
    third = _run(p, store)
    assert 0 < third.profile.columns_profiled <= 2  # only the edited table's columns
    assert third.profile.columns_reused > 0
    assert 0 < third.verified < first.verified and third.reused > 0
    assert _pair(third.joins, "inspections", "ロットNO", "lots", "lot_no")


def test_store_rows_are_bounded_json_evidence(report):
    for j in report.joins:
        assert 0.0 <= j["confidence"] <= 1.0
        json.dumps(j["evidence"])


# ------------------------------------------------------------------ pure helpers
def test_shape_signatures():
    assert shape_of("T5-03-06NA-01") == "AN-NN-NNAA-NN"
    assert shape_of("2024-0001") == "NNNN-NNNN"
    assert shape_of("ＡＢ１２") == "WWWW"
    assert shape_of("設備") == "WW"
    assert collapse_shape("NNNN-NN") == "N-N"


def test_name_tokens():
    assert name_tokens("lot_no") == ["lot", "no"]
    assert name_tokens("LotNo") == ["lot", "no"]
    assert name_tokens("ロットNO") == ["ロット", "no"]
    assert name_tokens("ロットＮＯ") == ["ロット", "no"]


def test_odbc_sql_generation_is_pure_and_bounded():
    assert quote_ident("mssql", "dbo.my]t") == "[dbo].[my]]t]"
    sql = sql_select_in("mssql", "dbo.t", "c", 3)
    assert sql == "SELECT DISTINCT [c] FROM [dbo].[t] WHERE [c] IN (?,?,?)"
    with pytest.raises(ValueError):
        sql_select_in("mssql", "dbo.t", "c", 5000)
    big = sql_sample_mssql("dbo.t", ["a", "b"], 1000, 50_000_000)
    assert big.startswith("SELECT TOP (1000) [a], [b] FROM [dbo].[t] TABLESAMPLE (")
    assert "NEWID" not in big.upper() and "ORDER BY" not in big.upper()
    assert "TABLESAMPLE" not in sql_sample_mssql("dbo.t", ["a"], 1000, 500)
    assert "sys.dm_db_partition_stats" in sql_rowcount_mssql()


def test_sqlite_source_is_read_only(db):
    src = SqliteSource(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            src._con.execute("INSERT INTO lots VALUES ('x','y')")
    finally:
        src.close()
    with pytest.raises(ValueError):
        open_source("nonsense")


def test_cli_prints_a_table(db, tmp_path, capsys):
    out = tmp_path / "cli.sqlite"
    rc = main(["--source", f"sqlite:{db}", "--out", str(out), "--max-pairs", "50",
               "--sample-rows", "2000", "--alias", "lot=ロット"])
    text = capsys.readouterr().out
    assert rc == 0 and "order_items.sku" in text and "products.sku" in text
    assert "conf" in format_joins([])
