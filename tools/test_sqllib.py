"""Tests for tools/sqllib: the confirmed-SQL library (synthetic data only)."""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from tools import security as sec
from tools.sqllib import __main__ as cli
from tools.sqllib import match, mcp_tools, sqlcheck, store, textnorm

SQL_A = "SELECT region, SUM(amount) AS total FROM sales WHERE sold_at >= :start_date GROUP BY region"
Q_A = "先月の地域別の売上合計を教えてください"


@pytest.fixture(autouse=True)
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv(store.ENV_DB, str(tmp_path / "lib.sqlite"))
    monkeypatch.delenv(store.ENV_VALIDATE_N, raising=False)
    yield tmp_path


@pytest.fixture
def unlocked(monkeypatch):
    monkeypatch.setattr(mcp_tools, "require_unlocked", lambda: None)


def _save(q=Q_A, sql=SQL_A, **kw):
    return store.save(q, sql, kw.pop("params", {"start_date": "period start"}), **kw)


def _run(i, h="r1", rows=5, args="a", outcome="ok"):
    return store.record_use(i, outcome, rowcount=rows, ms=10, result_hash=h, args_hash=args)


# ---------------------------------------------------------------- configuration

def test_unset_db_is_an_explicit_error(monkeypatch):
    monkeypatch.delenv(store.ENV_DB)
    with pytest.raises(store.SqllibNotConfigured) as ei:
        store.save("q", "SELECT 1")
    assert "MCP_SQLLIB_DB" in str(ei.value)


def test_unset_db_through_the_tool(monkeypatch, unlocked):
    monkeypatch.delenv(store.ENV_DB)
    out = mcp_tools.sql_examples_search("anything")
    assert out.startswith("[sql_examples_search error") and "MCP_SQLLIB_DB" in out


# ---------------------------------------------------------------- normalisation + matching

def test_fullwidth_halfwidth_and_case_fold():
    assert textnorm.normalize("ＳＡＬＥＳ　Ｒｅｇｉｏｎ １２３") == textnorm.normalize("sales region 123")


def test_relative_periods_share_one_token():
    a = textnorm.normalize("過去1か月の売上")
    b = textnorm.normalize("先月の売上")
    c = textnorm.normalize("過去３ヶ月の売上")
    assert a == b == c
    assert textnorm.REL in a


def test_dates_and_numbers_are_replaced():
    assert textnorm.DATE in textnorm.normalize("2024年1月5日の売上")
    assert textnorm.normalize("2024-01-05 sales") == textnorm.normalize("2025/12/31 sales")
    assert textnorm.NUM in textnorm.normalize("上位10件")


def test_match_labels_none_draft_validated_certified():
    assert match.match("anything at all")["verdict"] == "none"
    e = _save()
    r = match.match(Q_A)
    assert r["verdict"] == "draft" and r["matches"][0]["id"] == e["id"]
    assert "unverified" in r["label"] and "caution" in r
    _run(e["id"]); _run(e["id"])
    r = match.match(Q_A)
    assert r["verdict"] == "validated"
    assert "NOT confirmed" in r["label"] and "business logic" in r["caution"]
    store.feedback(e["id"], "ok", source="user")
    r = match.match(Q_A)
    assert r["verdict"] == "certified" and "caution" not in r


def test_match_survives_notation_variants():
    _save()
    for variant in ("過去1か月の地域別売上合計は？", "地域別の売上合計を見せて 過去30日",
                    "先月の地域別 売上合計"):
        assert match.match(variant)["verdict"] == "draft", variant


def test_match_ascii_questions_with_fullwidth():
    store.save("total sales by region for last month", SQL_A, {"start_date": ""})
    assert match.match("Total  Sales by Region for last month")["verdict"] == "draft"
    assert match.match("ｔｏｔａｌ　ｓａｌｅｓ ｂｙ ｒｅｇｉｏｎ ｆｏｒ ｐａｓｔ 3 ｍｏｎｔｈｓ")["verdict"] == "draft"


def test_unrelated_question_does_not_match():
    _save()
    assert match.match("従業員の平均勤続年数")["verdict"] == "none"


def test_threshold_is_adjustable():
    _save()
    q = "地域別の在庫"
    assert match.match(q)["verdict"] == "none"
    assert match.match(q, threshold=0.1)["verdict"] == "draft"


def test_higher_tier_is_listed_first_and_sets_the_verdict():
    d = _save()
    v = _save(sql=SQL_A + " ORDER BY region")
    _run(v["id"]); _run(v["id"])
    r = match.match(Q_A)
    assert r["verdict"] == "validated"
    assert [m["id"] for m in r["matches"]][0] == v["id"]
    assert {m["id"] for m in r["matches"]} == {d["id"], v["id"]}


def test_deprecated_is_hidden_unless_asked():
    e = _save()
    store.deprecate(e["id"])
    assert match.match(Q_A)["matches"] == []
    r = match.match(Q_A, include_deprecated=True)
    assert r["matches"][0]["status"] == "deprecated" and r["verdict"] == "none"


# ---------------------------------------------------------------- SQL inspection

@pytest.mark.parametrize("sql,code", [
    ("SELECT 1; SELECT 2", "multiple_statements"),
    ("SELECT 1; DROP TABLE t", "multiple_statements"),
    ("UPDATE t SET a=1", "not_a_read_query"),
    ("DELETE FROM t", "not_a_read_query"),
    ("INSERT INTO t VALUES (1)", "not_a_read_query"),
    ("SELECT a INTO backup FROM t", "forbidden_keyword"),
    ("WITH x AS (SELECT 1) DELETE FROM t", "forbidden_keyword"),
    ("SELECT 1 -- hide", "comments_not_allowed"),
    ("SELECT /* x */ 1", "comments_not_allowed"),
    ("SELECT 1 */", "comments_not_allowed"),
    ("", "empty_sql"),
])
def test_sql_rejections(sql, code):
    rep = sqlcheck.check(sql)
    assert not rep.ok and any(e.startswith(code) for e in rep.errors), rep.errors


def test_sql_accepts_reads_and_ignores_literals_that_look_like_syntax():
    assert sqlcheck.check("SELECT a FROM t;").ok
    assert sqlcheck.check("WITH x AS (SELECT 1 AS a) SELECT a FROM x").ok
    rep = sqlcheck.check("SELECT 'a;b -- not a comment /* nor this */' AS s FROM t")
    assert rep.ok, rep.errors
    assert sqlcheck.check("SELECT \"update\" FROM t").ok


def test_literal_values_get_a_parameterization_warning():
    rep = sqlcheck.check("SELECT * FROM t WHERE d >= '2024-01-01' AND n > 500")
    assert rep.ok and rep.hardcoded
    assert any("hardcoded_literal" in w and "date literal" in w and "number literal 500" in w
               for w in rep.warnings)
    clean = sqlcheck.check("SELECT * FROM t WHERE d >= :d AND n > :n LIMIT 10", {"d": 1, "n": 1})
    assert clean.ok and not clean.hardcoded and clean.params_used == ["d", "n"]


def test_parameter_declaration_warnings():
    rep = sqlcheck.check("SELECT * FROM t WHERE a = :a", {"b": {}})
    assert any(w.startswith("undeclared_parameter") for w in rep.warnings)
    assert any(w.startswith("unused_parameter") for w in rep.warnings)
    assert any(w.startswith("use_named") for w in sqlcheck.check("SELECT * FROM t WHERE a = ?").warnings)


def test_save_rejects_bad_sql_and_flags_literals():
    with pytest.raises(ValueError):
        store.save("q", "DROP TABLE t")
    r = store.save("q literal", "SELECT * FROM t WHERE d >= '2024-01-01'")
    assert r["status"] == "draft" and any("hardcoded" in w for w in r["warnings"])
    assert "parameterization recommended" in store.get(r["id"])["notes"]


def test_tables_are_derived():
    e = store.get(_save()["id"])
    assert e["tables"] == ["sales"]


def test_duplicate_save_returns_existing():
    a = _save()
    b = _save()
    assert b["duplicate"] and b["id"] == a["id"]
    assert len(store.list_entries()) == 1


# ---------------------------------------------------------------- auto validation

def test_promotes_after_two_identical_successful_runs():
    i = _save()["id"]
    assert not _run(i)["promoted"] and store.get(i)["status"] == "draft"
    res = _run(i)
    assert res["promoted"] and res["status"] == "validated" and res["matching_runs"] == 2
    e = store.get(i)
    assert e["status"] == "validated" and e["use_count"] == 2 and e["validated_at"]


def test_threshold_is_configurable(monkeypatch):
    monkeypatch.setenv(store.ENV_VALIDATE_N, "3")
    i = _save()["id"]
    _run(i); assert not _run(i)["promoted"]
    assert _run(i)["promoted"]


def test_no_promotion_when_rerun_result_differs():
    i = _save()["id"]
    _run(i, h="r1")
    res = _run(i, h="r2")
    assert not res["promoted"] and store.get(i)["status"] == "draft"
    assert _run(i, h="r2")["promoted"]  # two matching runs in a row now


def test_no_promotion_on_empty_result_error_or_missing_fingerprint():
    i = _save()["id"]
    _run(i, rows=0); _run(i, rows=0)
    _run(i, outcome="error"); _run(i, outcome="error")
    store.record_use(i, "ok", rowcount=3, ms=1)          # no result_hash
    store.record_use(i, "ok", rowcount=3, ms=1)
    assert store.get(i)["status"] == "draft"


def test_an_error_between_runs_resets_the_streak():
    i = _save()["id"]
    _run(i); _run(i, outcome="error")
    assert not _run(i)["promoted"]


def test_runs_with_different_arguments_are_not_compared():
    i = _save()["id"]
    _run(i, h="r1", args="x"); _run(i, h="r2", args="y")
    assert store.get(i)["status"] == "draft"
    assert _run(i, h="r2", args="y")["promoted"]


def test_bad_outcome_and_unknown_id():
    i = _save()["id"]
    with pytest.raises(ValueError):
        store.record_use(i, "weird")
    with pytest.raises(KeyError):
        store.record_use(999, "ok")


# ---------------------------------------------------------------- editing, history

def _validated():
    i = _save()["id"]
    _run(i); _run(i)
    assert store.get(i)["status"] == "validated"
    return i


def test_editing_sql_returns_to_draft_and_keeps_history():
    i = _validated()
    res = store.update_entry(i, sql=SQL_A + " ORDER BY total DESC")
    assert res["demoted"] and res["status"] == "draft"
    e = store.get(i)
    assert e["status"] == "draft" and e["validated_at"] is None
    h = store.history(i)
    assert len(h) == 1 and h[0]["sql"] == SQL_A and h[0]["status"] == "validated"
    # the old runs were of the old SQL: they do not count toward the new one
    assert not _run(i)["promoted"]
    assert _run(i)["promoted"]


def test_editing_certified_also_returns_to_draft():
    i = _validated()
    store.certify(i, "someone")
    store.update_entry(i, sql=SQL_A.replace("SUM", "AVG"))
    assert store.get(i)["status"] == "draft" and store.get(i)["certified_by"] is None


def test_non_sql_edit_keeps_the_status():
    i = _validated()
    res = store.update_entry(i, notes="clarified wording", question=Q_A + " 全部")
    assert not res["changed"] and store.get(i)["status"] == "validated"
    assert store.history(i) == []


def test_whitespace_only_edit_is_not_a_change():
    i = _validated()
    res = store.update_entry(i, sql="SELECT region,   SUM(amount) AS total\nFROM sales "
                                    "WHERE sold_at >= :start_date GROUP BY region;")
    assert not res["changed"] and store.get(i)["status"] == "validated"


def test_editing_params_declaration_counts_as_a_change():
    i = _validated()
    res = store.update_entry(i, params={"start_date": "different meaning"})
    assert res["demoted"]


def test_edit_with_bad_sql_is_refused_and_changes_nothing():
    i = _validated()
    with pytest.raises(ValueError):
        store.update_entry(i, sql="DELETE FROM sales")
    assert store.get(i)["status"] == "validated"


def test_tampering_behind_the_api_does_not_keep_a_confirmed_status(lib):
    i = _validated()
    con = sqlite3.connect(str(lib / "lib.sqlite"))
    con.execute("UPDATE entries SET sql=?, content_hash='tampered' WHERE id=?", ("SELECT 1", i))
    con.commit(); con.close()
    assert store.get(i)["status"] == "draft"
    assert match.match(Q_A)["verdict"] == "draft"


# ---------------------------------------------------------------- feedback

def test_user_ok_certifies_from_draft_and_validated():
    d = _save()["id"]
    assert store.feedback(d, "ok", source="user")["status"] == "certified"
    v = _validated_other()
    assert store.feedback(v, "ok", "looks right", source="user")["status"] == "certified"


def _validated_other():
    i = _save("在庫の商品別合計", "SELECT item, SUM(qty) FROM stock GROUP BY item")["id"]
    _run(i); _run(i)
    return i


def test_auto_ok_does_not_certify():
    i = _save()["id"]
    assert store.feedback(i, "ok", source="auto")["status"] == "draft"


def test_ng_flags_review_and_drops_trust():
    i = _validated()
    res = store.feedback(i, "ng", "wrong region split", source="user")
    assert res["needs_review"] and res["status"] == "draft"
    assert store.get(i)["needs_review"]
    assert match.match(Q_A)["matches"][0]["needs_review"] is True
    # an OK from a user clears the flag
    assert not store.feedback(i, "ok", source="user")["needs_review"]


def test_feedback_validation_and_deprecated_is_untouched():
    i = _save()["id"]
    with pytest.raises(ValueError):
        store.feedback(i, "maybe")
    with pytest.raises(ValueError):
        store.feedback(i, "ok", source="robot")
    store.deprecate(i)
    assert store.feedback(i, "ok", source="user")["status"] == "deprecated"


def test_certify_needs_a_name_and_not_deprecated():
    i = _save()["id"]
    with pytest.raises(ValueError):
        store.certify(i, "  ")
    store.deprecate(i)
    with pytest.raises(ValueError):
        store.certify(i, "x")


# ---------------------------------------------------------------- MCP tools

def test_tools_are_gated_when_locked():
    req = MagicMock()
    req.client.host = "198.51.100.40"
    req.headers.get = lambda k, d="": d
    with patch("tools.security.get_http_request", return_value=req):
        for fn, args in ((mcp_tools.sql_examples_search, ("q",)),
                         (mcp_tools.sql_example_save, ("q", "SELECT 1")),
                         (mcp_tools.sql_example_use, (1, "ok")),
                         (mcp_tools.sql_example_feedback, (1, "ok"))):
            assert fn(*args).startswith("[locked"), fn.__name__


def test_tool_roundtrip(unlocked):
    saved = json.loads(mcp_tools.sql_example_save(Q_A, SQL_A, {"start_date": "x"}, None, "n"))
    assert saved["status"] == "draft"
    found = json.loads(mcp_tools.sql_examples_search("先月の地域別売上合計"))
    assert found["verdict"] == "draft" and found["matches"][0]["id"] == saved["id"]
    mcp_tools.sql_example_use(saved["id"], "ok", 5, 10, "h", "a")
    used = json.loads(mcp_tools.sql_example_use(saved["id"], "ok", 5, 12, "h", "a"))
    assert used["promoted"] and used["status"] == "validated"
    fb = json.loads(mcp_tools.sql_example_feedback(saved["id"], "ok", "good", "user"))
    assert fb["status"] == "certified"


def test_save_tool_cannot_set_a_status(unlocked):
    import inspect
    assert "status" not in inspect.signature(mcp_tools.sql_example_save).parameters
    with pytest.raises(TypeError):
        mcp_tools.sql_example_save(Q_A, SQL_A, status="certified")
    out = json.loads(mcp_tools.sql_example_save(Q_A, SQL_A))
    assert out["status"] == "draft"


def test_tool_reports_bad_input_as_text(unlocked):
    assert mcp_tools.sql_example_save("q", "DROP TABLE x").startswith("[sql_example_save error")
    assert mcp_tools.sql_example_use(77, "ok").startswith("[sql_example_use error")
    assert mcp_tools.sql_example_feedback(77, "ok").startswith("[sql_example_feedback error")


def test_no_tool_certifies_or_deprecates_by_name():
    names = {f.__name__ for f in mcp_tools.SQLLIB_TOOLS}
    assert names == {"sql_examples_search", "sql_example_save", "sql_example_use",
                     "sql_example_feedback"}
    assert not any(("certify" in n or "deprecate" in n or "verify" in n) for n in names)


def test_registered_in_main_and_catalogue():
    from tools.tool_catalogue import categorise
    for n in ("sql_examples_search", "sql_example_save", "sql_example_use", "sql_example_feedback"):
        assert categorise(n) == "data"
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    assert "sql_example_feedback" in src and src.count("sql_examples_search") >= 2


# ---------------------------------------------------------------- CLI

def test_cli_certify_deprecate_list_show_edit(lib, capsys):
    i = _save()["id"]
    assert cli.main(["list", "--status", "draft"]) == 0
    assert str(i) in capsys.readouterr().out
    assert cli.main(["certify", str(i), "--by", "Reviewer"]) == 0
    assert store.get(i)["status"] == "certified" and store.get(i)["certified_by"] == "Reviewer"
    sqlf = lib / "new.sql"
    sqlf.write_text(SQL_A + " ORDER BY 2", encoding="utf-8")
    assert cli.main(["edit", str(i), "--sql-file", str(sqlf)]) == 0
    assert store.get(i)["status"] == "draft"
    assert cli.main(["show", str(i)]) == 0
    assert "history" in capsys.readouterr().out
    assert cli.main(["deprecate", str(i), "--note", "obsolete"]) == 0
    assert store.get(i)["status"] == "deprecated"


def test_cli_errors_are_reported_not_raised(monkeypatch, capsys):
    assert cli.main(["show", "404"]) == 2
    monkeypatch.delenv(store.ENV_DB)
    assert cli.main(["list"]) == 2
    assert "MCP_SQLLIB_DB" in capsys.readouterr().err


def test_import_export_roundtrip_and_status_trust(lib):
    i = _validated()
    store.certify(i, "Reviewer")
    out = lib / "x.jsonl"
    assert store.export_jsonl(out) == 1
    rec = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert rec["status"] == "certified"
    # a different library, default import: everything lands as draft
    import os
    os.environ[store.ENV_DB] = str(lib / "other.sqlite")
    assert store.import_jsonl(out)["added"] == 1
    assert store.list_entries()[0]["status"] == "draft"
    # trusted import keeps the status
    os.environ[store.ENV_DB] = str(lib / "third.sqlite")
    assert store.import_jsonl(out, trust_status=True, by="Operator")["added"] == 1
    assert store.list_entries()[0]["status"] == "certified"
    # duplicate and bad lines
    bad = lib / "bad.jsonl"
    bad.write_text(out.read_text(encoding="utf-8") + '{"question":"q","sql":"DROP TABLE t"}\n',
                   encoding="utf-8")
    res = store.import_jsonl(bad)
    assert res["skipped_duplicates"] == 1 and len(res["errors"]) == 1
