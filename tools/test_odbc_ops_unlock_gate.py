"""odbc_query / odbc_tables / odbc_columns now require unlock, same as odbc_to_excel
already did.

Owner's decision (2026-09-24): a database read returns the same kind of sensitive
business data a write would, so these three sit behind the same per-IP unlock.
odbc_drivers (local driver names) and odbc_connections (named connections, target
masked) are left ungated -- they are configuration discovery, not live data -- and
this file pins that split too. Runtime behaviour is checked through the SAME
HTTP-context path tools/test_security.py uses for every other gated tool (patching
tools.security.get_http_request).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tools import security as sec
import tools.odbc_ops as D


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(sec, "STATE_FILE", tmp_path / ".unlock_state.json")
    yield


def _make_req(peer_host: str, xff: str = "") -> MagicMock:
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = peer_host
    headers = {}
    if xff:
        headers["x-forwarded-for"] = xff
    req.headers = MagicMock()
    req.headers.get = lambda key, default="": headers.get(key.lower(), default)
    return req


def _fake_query_connection():
    con = MagicMock()
    cur = MagicMock()
    cur.description = [("id",), ("name",)]
    cur.fetchmany.return_value = [(1, "row one")]
    con.cursor.return_value = cur
    return con


def _fake_tables_connection():
    con = MagicMock()
    cur = MagicMock()
    row = MagicMock(table_cat="db", table_schem="dbo", table_name="Customers", table_type="TABLE")
    cur.tables.return_value = [row]
    con.cursor.return_value = cur
    return con


def _fake_columns_connection():
    con = MagicMock()
    cur = MagicMock()
    row = MagicMock(column_name="CustomerId", type_name="int", column_size=4, nullable=0)
    cur.columns.return_value = [row]
    con.cursor.return_value = cur
    return con


# ── odbc_query ───────────────────────────────────────────────────────────────────

def test_odbc_query_refuses_when_locked():
    req = _make_req(peer_host="198.51.100.40", xff="")
    with patch("tools.security.get_http_request", return_value=req):
        result = D.odbc_query("mydb", "SELECT 1")
    assert result.startswith("[locked")


def test_odbc_query_reads_after_unlock(monkeypatch):
    monkeypatch.setattr(D, "_connect", lambda *a, **kw: _fake_query_connection())
    granted = sec.grant_ip("198.51.100.41")
    req = _make_req(peer_host="127.0.0.1", xff="198.51.100.41")
    sec.set_presented_token(granted["unlock_token"])
    try:
        with patch("tools.security.get_http_request", return_value=req):
            result = D.odbc_query("mydb", "SELECT * FROM customers")
    finally:
        sec.clear_presented_token()
    assert "row one" in result


# ── odbc_tables ──────────────────────────────────────────────────────────────────

def test_odbc_tables_refuses_when_locked():
    req = _make_req(peer_host="198.51.100.42", xff="")
    with patch("tools.security.get_http_request", return_value=req):
        result = D.odbc_tables("mydb")
    assert result.startswith("[locked")


def test_odbc_tables_reads_after_unlock(monkeypatch):
    monkeypatch.setattr(D, "_connect", lambda *a, **kw: _fake_tables_connection())
    granted = sec.grant_ip("198.51.100.43")
    req = _make_req(peer_host="127.0.0.1", xff="198.51.100.43")
    sec.set_presented_token(granted["unlock_token"])
    try:
        with patch("tools.security.get_http_request", return_value=req):
            result = D.odbc_tables("mydb")
    finally:
        sec.clear_presented_token()
    assert "Customers" in result


# ── odbc_columns ─────────────────────────────────────────────────────────────────

def test_odbc_columns_refuses_when_locked():
    req = _make_req(peer_host="198.51.100.44", xff="")
    with patch("tools.security.get_http_request", return_value=req):
        result = D.odbc_columns("mydb", "Customers")
    assert result.startswith("[locked")


def test_odbc_columns_reads_after_unlock(monkeypatch):
    monkeypatch.setattr(D, "_connect", lambda *a, **kw: _fake_columns_connection())
    granted = sec.grant_ip("198.51.100.45")
    req = _make_req(peer_host="127.0.0.1", xff="198.51.100.45")
    sec.set_presented_token(granted["unlock_token"])
    try:
        with patch("tools.security.get_http_request", return_value=req):
            result = D.odbc_columns("mydb", "Customers")
    finally:
        sec.clear_presented_token()
    assert "CustomerId" in result


# ── the deliberately-left-ungated pair: still no gate ────────────────────────────

def test_odbc_drivers_stays_ungated():
    """Configuration discovery, not live data -- must not require unlock."""
    req = _make_req(peer_host="198.51.100.46", xff="")
    with patch("tools.security.get_http_request", return_value=req):
        result = D.odbc_drivers()
    assert not result.startswith("[locked")


def test_odbc_connections_stays_ungated():
    """Named connections with the target masked -- must not require unlock."""
    req = _make_req(peer_host="198.51.100.47", xff="")
    with patch("tools.security.get_http_request", return_value=req):
        result = D.odbc_connections()
    assert not result.startswith("[locked")


# ── odbc_to_excel was already gated; confirm this change did not disturb it ──────

def test_odbc_to_excel_still_gated_unchanged():
    req = _make_req(peer_host="198.51.100.48", xff="")
    with patch("tools.security.get_http_request", return_value=req):
        result = D.odbc_to_excel("mydb", "SELECT 1", "out.xlsx")
    assert result.startswith("[locked")
