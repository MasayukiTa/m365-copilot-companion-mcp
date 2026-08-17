"""`list_unlocked` answers about the caller, not about everybody else.

WHY IT IS SCOPED. The tool is registered as an ordinary MCP tool and is not itself behind the
unlock gate, and it used to return the whole table of unlocked addresses. Anything that hands
an unauthenticated-for-this-purpose caller a list of the identities the authorisation check
keys on is doing part of an attacker's work, whatever the rest of the design looks like. It
now answers only for the caller, which is the question the one legitimate consumer asks
(`bench/swe_unlock_bootstrap.py` unlocks and then checks whether it worked) and which tells a
caller nothing it did not already have.

A genuine local caller -- loopback peer, no forwarding header -- still gets the whole table.
That is the operator at the machine, and the same file is on their disk.

THIS IS A NARROWING, NOT A FIX. The unlock gate keys on an IP derived from a request header,
and an IP is a value a caller states rather than proves. `test_identity_is_still_derived_from
_a_client_supplied_value` at the bottom asserts that this remains true, so that a green suite
is never read as an all-clear. Details of the consequences are deliberately not written down
here: this repository is public and the gate is still open. They are in the private report.
"""
import json
import time

import pytest

from tools import security as S


CALLER = "203.0.113.77"      # RFC 5737 documentation range, never routable
STRANGER = "198.51.100.9"


class _Req:
    def __init__(self, peer, xff=""):
        self.client = type("c", (), {"host": peer})()
        self.headers = {"x-forwarded-for": xff} if xff else {}


@pytest.fixture
def state(tmp_path, monkeypatch):
    path = tmp_path / "unlock.json"
    path.write_text(json.dumps({
        CALLER: {"expires_at": time.time() + 86400},
        STRANGER: {"expires_at": time.time() + 86400},
    }), encoding="utf-8")
    monkeypatch.setattr(S, "STATE_FILE", path)
    return path


def _as(monkeypatch, peer, xff=""):
    monkeypatch.setattr(S, "get_http_request", lambda: _Req(peer, xff))


def test_a_remote_caller_is_told_about_itself_and_nobody_else(state, monkeypatch):
    _as(monkeypatch, "127.0.0.1", xff=CALLER)
    out = S.list_unlocked()
    assert CALLER in out
    assert STRANGER not in out, "another party's entry was disclosed"


def test_a_caller_with_no_entry_learns_only_that(state, monkeypatch):
    _as(monkeypatch, "10.0.0.5", xff="192.0.2.1")
    out = S.list_unlocked()
    assert "not unlocked" in out
    assert CALLER not in out and STRANGER not in out


def test_the_table_cannot_be_enumerated_in_one_call(state, monkeypatch):
    _as(monkeypatch, "203.0.113.200")
    out = S.list_unlocked()
    for ip in (CALLER, STRANGER):
        assert ip not in out


def test_the_operator_at_the_machine_still_sees_the_table(state, monkeypatch):
    _as(monkeypatch, "127.0.0.1")
    out = S.list_unlocked()
    assert CALLER in out and STRANGER in out


def test_the_cli_path_without_an_http_context_still_sees_the_table(state, monkeypatch):
    def _boom():
        raise RuntimeError("no request context")

    monkeypatch.setattr(S, "get_http_request", _boom)
    out = S.list_unlocked()
    assert CALLER in out and STRANGER in out


def test_the_bootstrap_check_still_works(state, monkeypatch):
    """Unlock, then ask whether it took. That question is about the caller, so it is answered."""
    _as(monkeypatch, "127.0.0.1", xff=CALLER)
    assert "days remaining" in S.list_unlocked()


def test_identity_is_still_derived_from_a_client_supplied_value(state, monkeypatch):
    """THE PART THAT IS NOT FIXED, asserted so the suite cannot be read as an all-clear.

    When this starts failing, delete this test rather than inverting it -- and update
    docs/SECURITY.md and the README in the same change, because both currently describe a
    property the code does not have.
    """
    _, ip = S.derive_identity("127.0.0.1", CALLER)
    assert ip == CALLER, "identity no longer comes from the header"
    assert S.is_unlocked(ip) is True, "the gate no longer keys on it -- see the docstring"
