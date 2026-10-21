"""既定で行数を切らず、接続待ちも制限しないことを証かめる。

それぞれ呼び出し側が max_rows / timeout を明示したときだけ、その値で切る。
実 DB には触れない。_connect / require_unlocked / _write_dataframe_to_excel は
すべて monkeypatch し、FAKE 識別子のみを使う。
"""
from __future__ import annotations

import pytest

from tools import odbc_ops


class _FakeCursor:
    """fetchmany(n) と fetchall() を分けて記録する fake cursor。"""

    def __init__(self, columns, rows):
        self._columns = columns
        self._rows = rows
        self.description = [(c,) for c in columns]
        self.fetchmany_calls = []
        self.fetchall_calls = 0

    def execute(self, sql, *params):
        pass

    def fetchmany(self, n):
        self.fetchmany_calls.append(n)
        return self._rows[:n]

    def fetchall(self):
        self.fetchall_calls += 1
        return self._rows


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _bypass_unlock(monkeypatch):
    monkeypatch.setattr(odbc_ops, "require_unlocked", lambda: None)


def _patch_connect(monkeypatch, cursor):
    con = _FakeConnection(cursor)
    monkeypatch.setattr(odbc_ops, "_connect", lambda connection, timeout=odbc_ops.DEFAULT_TIMEOUT: con)
    return con


# --- (c) max_rows の既定は「切らない」 ---------------------------------------

def test_odbc_query_by_default_fetches_every_row(monkeypatch):
    """既定（max_rows 未指定）では fetchall を使い、600 行全てが返る。
    truncated 行は出ない。"""
    columns = ["fake_col_a", "fake_col_b"]
    rows = [(f"fake_{i}", i) for i in range(600)]
    cursor = _FakeCursor(columns, rows)
    _patch_connect(monkeypatch, cursor)

    out = odbc_ops.odbc_query("demo_db", "SELECT fake_col_a, fake_col_b FROM FAKE_TABLE_A")

    assert cursor.fetchall_calls == 1
    assert cursor.fetchmany_calls == []
    assert "--- 600 row(s)" in out
    assert "truncated at max_rows" not in out


def test_odbc_query_explicit_max_rows_still_cuts(monkeypatch):
    """呼び出し側が max_rows を明示したときだけ、fetchmany で切り truncated を記す。"""
    columns = ["fake_col_a"]
    rows = [(f"fake_{i}",) for i in range(600)]
    cursor = _FakeCursor(columns, rows)
    _patch_connect(monkeypatch, cursor)

    out = odbc_ops.odbc_query("demo_db", "SELECT fake_col_a FROM FAKE_TABLE_A", max_rows=200)

    assert cursor.fetchmany_calls == [200]
    assert cursor.fetchall_calls == 0
    assert "--- 200 row(s)" in out
    assert "truncated at max_rows=200" in out


def test_format_table_no_truncation_line_when_max_rows_is_none():
    """_format_table は max_rows=None で行数が一致しても truncated を出さない。"""
    columns = ["fake_col_a"]
    rows = [("x",), ("y",)]
    out = odbc_ops._format_table(columns, rows, None)
    assert "truncated at max_rows" not in out
    assert "--- 2 row(s)" in out


# --- (d) DEFAULT_TIMEOUT は「接続待ちを制限しない」 ----------------------------

def test_default_timeout_is_zero_meaning_no_connect_wait_cap():
    """既定の接続（ログイン）タイムアウトは 0 = ドライバ既定（上限なし）。"""
    assert odbc_ops.DEFAULT_TIMEOUT == 0


def test_connect_passes_the_timeout_through_to_pyodbc(monkeypatch):
    """_connect は受け取った timeout をそのまま pyodbc.connect に渡す。
    既定では 0、呼び出し側が明示したときはその値。"""
    seen = {}

    class _FakePyodbc:
        def connect(self, cs, timeout=None, readonly=None, autocommit=None):
            seen["timeout"] = timeout
            return _FakeConnection(_FakeCursor(["c"], []))

    monkeypatch.setitem(__import__("sys").modules, "pyodbc", _FakePyodbc())
    monkeypatch.setattr(
        odbc_ops, "_list_connections",
        lambda: {"demo_db": "Driver=x;Server=fakehost;Database=fake_db;"},
    )

    odbc_ops._connect("demo_db")
    assert seen["timeout"] == 0

    odbc_ops._connect("demo_db", timeout=12)
    assert seen["timeout"] == 12
