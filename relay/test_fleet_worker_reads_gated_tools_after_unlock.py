"""The fleet-worker auto-unlock flow (relay_fleet.RelayWorker) still recovers a
newly-gated READ tool the same way it already recovers a gated WRITE tool.

Context: 2026-09-24, clipboard_get / outlook_inbox / outlook_calendar / odbc_query /
odbc_tables / odbc_columns / registry_read moved behind require_unlocked(), same as
outlook_send_mail / odbc_to_excel already were. relay_fleet's auto-unlock detection
(_looks_locked / _decide, see relay/test_unlock_inject.py) keys off the LITERAL text
tools.security.require_unlocked() returns, not off which tool produced it -- so it
should already generalise to these without any relay change. This file proves that
end-to-end for the two tools this pass's phase-1 report singled out (mail, DB), using
the REAL refusal string produced by the REAL gate (not a hand-typed literal), with
Outlook/ODBC themselves stubbed so no live mailbox or database is touched.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from tools import security as sec
import tools.outlook_ops as O
import tools.odbc_ops as D

PW = "unit_test_pw_gated_reads"


@pytest.fixture(autouse=True)
def _unlock_env(monkeypatch):
    monkeypatch.setenv("MCP_UNLOCK_PASSWORD", PW)
    yield


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


def _real_locked_reply(call):
    """The ACTUAL refusal text require_unlocked() produces for a locked remote IP,
    obtained by calling the real gated tool -- not a literal copied by hand, which is
    exactly the drift risk relay/test_unlock_inject.py's own LOCKED constant carries."""
    req = _make_req(peer_host="203.0.113.99", xff="")
    with patch("tools.security.get_http_request", return_value=req):
        return call()


def test_relay_worker_auto_unlocks_on_a_real_outlook_inbox_refusal():
    import relay.relay_fleet as rf

    locked_reply = _real_locked_reply(lambda: O.outlook_inbox())
    assert locked_reply.startswith("[locked client IP:")

    w = rf.RelayWorker("mail の要約を作って", "u_gated_reads_1")
    before = w._unlock_attempts
    w._decide(locked_reply)
    assert w._unlock_attempts == before + 1
    assert "unlock" in (w.job or "") and PW in (w.job or "")
    assert "unlock_token" in (w.job or "")
    assert w.outcome is None and w.status != "stuck"


def test_relay_worker_auto_unlocks_on_a_real_odbc_query_refusal():
    import relay.relay_fleet as rf

    locked_reply = _real_locked_reply(lambda: D.odbc_query("mydb", "SELECT 1"))
    assert locked_reply.startswith("[locked client IP:")

    w = rf.RelayWorker("在庫テーブルを見て", "u_gated_reads_2")
    before = w._unlock_attempts
    w._decide(locked_reply)
    assert w._unlock_attempts == before + 1
    assert "unlock" in (w.job or "") and PW in (w.job or "")


def test_stubbed_outlook_inbox_actually_reads_once_the_worker_would_be_unlocked():
    """The other half: after the unlock relay_fleet injects would land (grant + token,
    the same effect unlock(password) has server-side), the SAME tool call that was
    refused now returns real data -- Outlook itself stubbed so no mailbox is touched."""
    m = MagicMock()
    m.UnRead = False
    m.SenderName = "Alice"
    m.SenderEmailAddress = "alice@example.com"
    m.Subject = "Q3 numbers"
    m.ReceivedTime = "2026-09-24T09:00"

    class _Items(list):
        def Sort(self, *a, **kw):
            pass

        @property
        def Count(self):
            return len(self)

        def Item(self, i):
            return self[i - 1]

    ol = MagicMock()
    ns = MagicMock()
    folder = MagicMock()
    folder.Items = _Items([m])
    ns.GetDefaultFolder.return_value = folder
    ol.GetNamespace.return_value = ns

    with patch.object(O, "_dispatch", lambda: ol), patch.object(O, "_release", lambda: None):
        granted = sec.grant_ip("203.0.113.100")
        req = _make_req(peer_host="127.0.0.1", xff="203.0.113.100")
        sec.set_presented_token(granted["unlock_token"])
        try:
            with patch("tools.security.get_http_request", return_value=req):
                result = O.outlook_inbox()
        finally:
            sec.clear_presented_token()
    assert "Q3 numbers" in result


def test_stubbed_odbc_query_actually_reads_once_the_worker_would_be_unlocked():
    con = MagicMock()
    cur = MagicMock()
    cur.description = [("id",), ("qty",)]
    cur.fetchmany.return_value = [(1, 42)]
    con.cursor.return_value = cur

    with patch.object(D, "_connect", lambda *a, **kw: con):
        granted = sec.grant_ip("203.0.113.101")
        req = _make_req(peer_host="127.0.0.1", xff="203.0.113.101")
        sec.set_presented_token(granted["unlock_token"])
        try:
            with patch("tools.security.get_http_request", return_value=req):
                result = D.odbc_query("mydb", "SELECT id, qty FROM stock")
        finally:
            sec.clear_presented_token()
    assert "42" in result
