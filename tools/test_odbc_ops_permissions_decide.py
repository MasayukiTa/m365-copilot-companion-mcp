# -*- coding: utf-8 -*-
"""The ODBC tools no longer force read-only: the database account's own permissions decide.

Owner decision 2026-10-20. MCP_DB_READONLY=1 is the opt-in for people who want the tool itself
to refuse everything but reads. Default off. A refusal written by the database (permission
denied) must reach the caller verbatim.
"""
from __future__ import annotations

import pytest

import tools.odbc_ops as D


class _Cur:
    description = None

    def __init__(self, fail=None):
        self.fail, self.ran = fail, []

    def execute(self, q, *params):
        self.ran.append(q)
        if self.fail:
            raise self.fail

    def fetchmany(self, n):
        return []


class _Con:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur

    def close(self):
        pass


@pytest.fixture(autouse=True)
def unlocked(monkeypatch):
    monkeypatch.setattr(D, "require_unlocked", lambda: None)
    monkeypatch.delenv("MCP_DB_READONLY", raising=False)


def _wire(monkeypatch, cur):
    monkeypatch.setattr(D, "_connect", lambda connection, timeout=30: _Con(cur))


def test_by_default_a_write_statement_is_sent_to_the_database(monkeypatch):
    cur = _Cur()
    _wire(monkeypatch, cur)
    out = D.odbc_query("demo", "UPDATE FAKE_TABLE_A SET x = 1")
    assert cur.ran == ["UPDATE FAKE_TABLE_A SET x = 1"]
    assert "only SELECT" not in out


def test_the_database_refusal_comes_back_verbatim(monkeypatch):
    msg = "('42000', \"[42000] [Microsoft][ODBC Driver 18] The UPDATE permission was denied on the object 'FAKE_TABLE_A' (229)\")"
    _wire(monkeypatch, _Cur(fail=RuntimeError(msg)))
    out = D.odbc_query("demo", "UPDATE FAKE_TABLE_A SET x = 1")
    assert msg in out and out.startswith("[odbc_query error: RuntimeError:")


def test_opt_in_refuses_non_reads_without_connecting(monkeypatch):
    monkeypatch.setenv("MCP_DB_READONLY", "1")
    monkeypatch.setattr(D, "_connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no connect")))
    out = D.odbc_query("demo", "DELETE FROM FAKE_TABLE_A")
    assert "only SELECT" in out and "MCP_DB_READONLY" in out
    assert "only SELECT" in D.odbc_to_excel("demo", "DROP TABLE FAKE_TABLE_A", "C:/fake/o.xlsx")


def test_opt_in_still_allows_reads(monkeypatch):
    monkeypatch.setenv("MCP_DB_READONLY", "1")
    cur = _Cur()
    _wire(monkeypatch, cur)
    D.odbc_query("demo", "SELECT 1 FROM FAKE_TABLE_A")
    assert cur.ran == ["SELECT 1 FROM FAKE_TABLE_A"]


@pytest.mark.parametrize("value,expected", [("", False), ("0", False), ("no", False),
                                            ("1", True), ("true", True), ("ON", True)])
def test_the_switch_reading(monkeypatch, value, expected):
    monkeypatch.setenv("MCP_DB_READONLY", value)
    assert D.readonly_enforced() is expected


def test_connection_opens_readonly_only_when_asked(monkeypatch):
    import sys, types
    seen = {}
    fake = types.SimpleNamespace(connect=lambda cs, **kw: seen.update(kw) or object())
    monkeypatch.setitem(sys.modules, "pyodbc", fake)
    monkeypatch.setenv("MCP_DB_DEMO", "Driver=x;Server=y")
    D._connect("demo")
    assert seen["readonly"] is False
    monkeypatch.setenv("MCP_DB_READONLY", "1")
    D._connect("demo")
    assert seen["readonly"] is True


def test_the_switch_is_not_a_named_connection(monkeypatch):
    monkeypatch.setenv("MCP_DB_READONLY", "1")
    monkeypatch.setenv("MCP_DB_DEMO", "Driver=x;Server=y")
    assert "readonly" not in D._list_connections() and "demo" in D._list_connections()
