"""outlook_inbox / outlook_calendar now require unlock, same as outlook_send_mail /
outlook_create_event already did.

Owner's decision (2026-09-24): reading the operator's mail/calendar is as sensitive
as writing it, so it sits behind the same per-IP unlock. Runtime behaviour is pinned
through the SAME HTTP-context path tools/test_security.py uses for every other gated
tool (patching tools.security.get_http_request).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tools import security as sec
import tools.outlook_ops as O


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


class _FakeItems(list):
    """Stands in for Outlook's Items collection: needs .Sort/.Count/.Item(1-based)/
    .IncludeRecurrences/.Restrict, matching what outlook_inbox/outlook_calendar use."""

    def Sort(self, *a, **kw):
        pass

    @property
    def Count(self):
        return len(self)

    def Item(self, i):
        return self[i - 1]

    IncludeRecurrences = False

    def Restrict(self, _restriction):
        return self


def _fake_ol(items):
    ol = MagicMock()
    ns = MagicMock()
    folder = MagicMock()
    folder.Items = items
    ns.GetDefaultFolder.return_value = folder
    ol.GetNamespace.return_value = ns
    return ol


def _one_mail_item():
    m = MagicMock()
    m.UnRead = False
    m.SenderName = "Alice"
    m.SenderEmailAddress = "alice@example.com"
    m.Subject = "Hello"
    m.ReceivedTime = "2026-09-24T09:00"
    return m


def _one_calendar_item():
    e = MagicMock()
    e.Start = "2026-09-24T10:00"
    e.End = "2026-09-24T10:30"
    e.Subject = "Standup"
    e.Location = "Room 1"
    e.Organizer = "Bob"
    return e


# ── outlook_inbox ────────────────────────────────────────────────────────────────

def test_outlook_inbox_refuses_when_locked():
    req = _make_req(peer_host="198.51.100.30", xff="")
    with patch("tools.security.get_http_request", return_value=req):
        result = O.outlook_inbox()
    assert result.startswith("[locked")


def test_outlook_inbox_reads_after_unlock(monkeypatch):
    monkeypatch.setattr(O, "_dispatch", lambda: _fake_ol(_FakeItems([_one_mail_item()])))
    monkeypatch.setattr(O, "_release", lambda: None)
    granted = sec.grant_ip("198.51.100.31")
    req = _make_req(peer_host="127.0.0.1", xff="198.51.100.31")
    sec.set_presented_token(granted["unlock_token"])
    try:
        with patch("tools.security.get_http_request", return_value=req):
            result = O.outlook_inbox()
    finally:
        sec.clear_presented_token()
    assert "Alice" in result and "Hello" in result


# ── outlook_calendar ─────────────────────────────────────────────────────────────

def test_outlook_calendar_refuses_when_locked():
    req = _make_req(peer_host="198.51.100.32", xff="")
    with patch("tools.security.get_http_request", return_value=req):
        result = O.outlook_calendar()
    assert result.startswith("[locked")


def test_outlook_calendar_reads_after_unlock(monkeypatch):
    monkeypatch.setattr(O, "_dispatch", lambda: _fake_ol(_FakeItems([_one_calendar_item()])))
    monkeypatch.setattr(O, "_release", lambda: None)
    granted = sec.grant_ip("198.51.100.33")
    req = _make_req(peer_host="127.0.0.1", xff="198.51.100.33")
    sec.set_presented_token(granted["unlock_token"])
    try:
        with patch("tools.security.get_http_request", return_value=req):
            result = O.outlook_calendar()
    finally:
        sec.clear_presented_token()
    assert "Standup" in result


# ── the two write tools were already gated; confirm this change did not disturb them ─

def test_outlook_send_mail_still_gated_unchanged():
    req = _make_req(peer_host="198.51.100.34", xff="")
    with patch("tools.security.get_http_request", return_value=req):
        result = O.outlook_send_mail(to="x@example.com", subject="s", body="b")
    assert result.startswith("[locked")


def test_outlook_create_event_still_gated_unchanged():
    req = _make_req(peer_host="198.51.100.35", xff="")
    with patch("tools.security.get_http_request", return_value=req):
        result = O.outlook_create_event(subject="s", start_iso="2026-09-24T10:00")
    assert result.startswith("[locked")
