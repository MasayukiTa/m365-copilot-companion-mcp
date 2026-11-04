"""DB 計数(db_counts)の単体試験。実 DB には接続せず runner を DI で差し替える。"""

from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts.weekly_scan import db_counts  # noqa: E402
from scripts.weekly_scan.db_counts import (  # noqa: E402
    DbCountError,
    count_lots,
    count_lots_by_hinmei,
    lots_shortfall,
)


def _runner_returning(text):
    """与えたテキストを返す擬似 odbc_query(connection, query, params=, max_rows=)。"""

    def _run(connection, query, params=None, max_rows=None):
        _run.last = {
            "connection": connection,
            "query": query,
            "params": params,
            "max_rows": max_rows,
        }
        return text

    _run.last = {}
    return _run


def test_count_lots_parses_single_value():
    run = _runner_returning("count\n-----\n42\n")
    assert count_lots("SELECT COUNT(*) FROM t", runner=run) == 42


def test_count_lots_strips_thousands_separator():
    run = _runner_returning("count\n-----\n18,829\n")
    assert count_lots("SELECT COUNT(*) FROM t", runner=run) == 18829


def test_count_lots_uses_default_connection_adams():
    run = _runner_returning("c\n-\n3\n")
    count_lots("SELECT 1", runner=run)
    assert run.last["connection"] == db_counts.DEFAULT_CONNECTION == "adams"


def test_count_lots_error_text_raises():
    run = _runner_returning("[odbc_query error: ProgrammingError: bad sql]")
    with pytest.raises(DbCountError):
        count_lots("SELECT bad", runner=run)


def test_count_lots_locked_refusal_raises():
    run = _runner_returning("This tool is locked. Call unlock(password=...) first.")
    with pytest.raises(DbCountError):
        count_lots("SELECT 1", runner=run)


def test_count_lots_empty_raises():
    run = _runner_returning("")
    with pytest.raises(DbCountError):
        count_lots("SELECT 1", runner=run)


def test_by_hinmei_parses_named_columns():
    text = (
        "品名            lots\n"
        "------------    ----\n"
        "E770G           2278\n"
        "705GLH          16551\n"
    )
    run = _runner_returning(text)
    got = count_lots_by_hinmei(
        "SELECT 品名, COUNT(DISTINCT ロットNO) AS lots FROM t GROUP BY 品名",
        hinmei_column="品名",
        count_column="lots",
        runner=run,
    )
    assert got == {"E770G": 2278, "705GLH": 16551}
    # 層別集計は全件取得(max_rows=None)で呼ぶこと。
    assert run.last["max_rows"] is None


def test_by_hinmei_pipe_separated_table():
    text = (
        "品名 | lots\n"
        "E770G | 10\n"
        "ABC | 20\n"
    )
    run = _runner_returning(text)
    got = count_lots_by_hinmei("SELECT ...", count_column="lots", runner=run)
    assert got == {"E770G": 10, "ABC": 20}


def test_by_hinmei_fallback_first_numeric_column():
    # count_column を指定しない場合、品名列以外の最初の整数列を件数とみなす。
    text = (
        "品名            n\n"
        "-----           --\n"
        "X               5\n"
    )
    run = _runner_returning(text)
    got = count_lots_by_hinmei("SELECT ...", runner=run)
    assert got == {"X": 5}


def test_by_hinmei_refusal_raises():
    run = _runner_returning("[odbc_query error: timeout]")
    with pytest.raises(DbCountError):
        count_lots_by_hinmei("SELECT ...", runner=run)


def test_shortfall():
    assert lots_shortfall(3, 8) == 5
    assert lots_shortfall(8, 8) == 0
    assert lots_shortfall(10, 8) == 0
    with pytest.raises(ValueError):
        lots_shortfall(1, -1)
