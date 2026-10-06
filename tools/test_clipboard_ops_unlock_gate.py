"""clipboard_get now requires unlock, same as clipboard_set already did.

Owner's decision (2026-09-24): the clipboard can hold anything the operator last
copied, so reading it sits behind the same per-IP unlock as a write. This pins the
runtime behaviour through the SAME HTTP-context path tools/test_security.py uses for
every other gated tool (patching tools.security.get_http_request), rather than
monkeypatching require_unlocked directly -- so a regression in the real gate would
show up here too.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tools import security as sec
import tools.clipboard_ops as C


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


def _fake_pyperclip(monkeypatch, paste_value="hello clipboard"):
    import sys
    import types

    mod = types.ModuleType("pyperclip")
    mod.paste = lambda: paste_value

    class PyperclipException(Exception):
        pass

    mod.PyperclipException = PyperclipException
    monkeypatch.setitem(sys.modules, "pyperclip", mod)
    return mod


def test_clipboard_get_refuses_when_locked():
    req = _make_req(peer_host="198.51.100.20", xff="")
    with patch("tools.security.get_http_request", return_value=req):
        result = C.clipboard_get()
    assert result.startswith("[locked")


def test_clipboard_get_reads_after_unlock(monkeypatch):
    _fake_pyperclip(monkeypatch, "secret text")
    granted = sec.grant_ip("198.51.100.21")
    req = _make_req(peer_host="127.0.0.1", xff="198.51.100.21")
    sec.set_presented_token(granted["unlock_token"])
    try:
        with patch("tools.security.get_http_request", return_value=req):
            result = C.clipboard_get()
    finally:
        sec.clear_presented_token()
    assert "secret text" in result


def test_clipboard_set_still_gated_unchanged():
    """clipboard_set was already gated before this change; confirm it still is."""
    req = _make_req(peer_host="198.51.100.22", xff="")
    with patch("tools.security.get_http_request", return_value=req):
        result = C.clipboard_set("x")
    assert result.startswith("[locked")
