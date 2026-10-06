"""registry_read now requires unlock.

Owner's decision (2026-09-24) named clipboard/mail/database explicitly; registry_read
is the one tool this pass added beyond that named set, because some registry
locations hold credential-adjacent data in plain text (e.g. an autologon password
under HKLM\\...\\Winlogon, or product keys) -- comparably sensitive to the named set.
Runtime behaviour is checked through the SAME HTTP-context path
tools/test_security.py uses for every other gated tool (patching
tools.security.get_http_request).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tools import security as sec
import tools.registry_ops as R


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


def test_registry_read_refuses_when_locked():
    req = _make_req(peer_host="198.51.100.50", xff="")
    with patch("tools.security.get_http_request", return_value=req):
        result = R.registry_read("HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion")
    assert result.startswith("[locked")


def test_registry_read_reads_after_unlock(monkeypatch):
    class _FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(R, "_open_key", lambda key_path: _FakeKey())

    import sys
    import types

    fake_winreg = types.ModuleType("winreg")
    fake_winreg.QueryValueEx = lambda k, name: ("1.2.3.4", 1)
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    granted = sec.grant_ip("198.51.100.51")
    req = _make_req(peer_host="127.0.0.1", xff="198.51.100.51")
    sec.set_presented_token(granted["unlock_token"])
    try:
        with patch("tools.security.get_http_request", return_value=req):
            result = R.registry_read("HKLM\\SOFTWARE\\X", value_name="Version")
    finally:
        sec.clear_presented_token()
    assert "1.2.3.4" in result


def test_service_status_stays_ungated():
    """service_status was not part of this pass's gated set -- confirm it still isn't."""
    req = _make_req(peer_host="198.51.100.52", xff="")
    with patch("tools.security.get_http_request", return_value=req):
        result = R.service_status()
    assert not result.startswith("[locked")
