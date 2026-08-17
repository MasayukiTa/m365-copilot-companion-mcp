"""The second key, made into something a caller must HOLD rather than something it states.

WHAT WAS WRONG. The unlock record was keyed on an identity derived from a forwarding header.
On this deployment nothing upstream appends its own hop, so the whole header is caller-supplied
and the identity is asserted rather than proved: possession of the API key was sufficient. The
documentation promised two independent keys; there was one.

WHAT REPLACES IT. `unlock(password)` issues a random token, stores only its hash, and returns
it once. Mutating and execution tools reach the server through one gateway, so the token is
presented as an argument there and stripped before dispatch -- 116 tool signatures unchanged.

ENFORCEMENT IS OFF BY DEFAULT and these tests pin that too. Requiring the token before anyone
has re-unlocked would refuse every live session at once, and an outage is how a security change
gets reverted wholesale instead of kept. The gap counter is what says when it is safe to turn
on; `test_a_call_without_a_token_is_counted` is the test that keeps that promise honest.
"""
import hashlib
import json
import time

import pytest

from tools import lock_state
from tools import security as S


IP = "203.0.113.77"       # RFC 5737 documentation range


class _Req:
    def __init__(self, peer="127.0.0.1", xff=IP):
        self.client = type("c", (), {"host": peer})()
        self.headers = {"x-forwarded-for": xff} if xff else {}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "STATE_FILE", tmp_path / "unlock.json")
    monkeypatch.setattr(lock_state, "_STATE_FILE", tmp_path / "lock.json")
    monkeypatch.setattr(lock_state, "_TOKEN_GAP_FILE", tmp_path / "gap.json")
    monkeypatch.setattr(S, "get_http_request", lambda: _Req())
    S.clear_presented_token()
    yield
    S.clear_presented_token()


def _unlock_with(monkeypatch, password="pw"):
    monkeypatch.setattr(S, "unlock_password_from_env", lambda: password)
    return S.unlock(password)


# ---- the token is issued, stored as a hash, and shown once ------------------------------

def test_unlock_returns_a_token_and_stores_only_its_hash(monkeypatch):
    out = _unlock_with(monkeypatch)
    token = [l.split(": ", 1)[1] for l in out.splitlines() if l.startswith("unlock_token: ")][0]
    assert len(token) >= 20
    entry = json.loads(S.STATE_FILE.read_text(encoding="utf-8"))[IP]
    assert entry["token_sha256"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in S.STATE_FILE.read_text(encoding="utf-8"), (
        "the state file is a list of things to impersonate if it holds the token itself")


def test_a_wrong_password_issues_nothing(monkeypatch):
    monkeypatch.setattr(S, "unlock_password_from_env", lambda: "pw")
    assert "failed" in S.unlock("not-the-password")
    assert not S.STATE_FILE.exists() or IP not in json.loads(
        S.STATE_FILE.read_text(encoding="utf-8"))


# ---- with enforcement on, the identity alone is not enough -------------------------------

def test_the_identity_alone_is_refused_when_enforcement_is_on(monkeypatch):
    """THE ATTACK. An API key plus a stated identity, with no token."""
    _unlock_with(monkeypatch)
    monkeypatch.setenv("MCP_REQUIRE_UNLOCK_TOKEN", "1")
    S.clear_presented_token()
    refusal = S.require_unlocked()
    assert refusal is not None and "unlock token" in refusal


def test_the_right_token_passes(monkeypatch):
    out = _unlock_with(monkeypatch)
    token = [l.split(": ", 1)[1] for l in out.splitlines() if l.startswith("unlock_token: ")][0]
    monkeypatch.setenv("MCP_REQUIRE_UNLOCK_TOKEN", "1")
    S.set_presented_token(token)
    assert S.require_unlocked() is None


def test_a_wrong_token_does_not(monkeypatch):
    _unlock_with(monkeypatch)
    monkeypatch.setenv("MCP_REQUIRE_UNLOCK_TOKEN", "1")
    S.set_presented_token("not-the-token")
    assert S.require_unlocked() is not None


def test_a_token_for_a_different_identity_does_not_transfer(monkeypatch):
    """Otherwise one unlocked party's token is a master key for anyone who can state an IP."""
    out = _unlock_with(monkeypatch)
    token = [l.split(": ", 1)[1] for l in out.splitlines() if l.startswith("unlock_token: ")][0]
    state = json.loads(S.STATE_FILE.read_text(encoding="utf-8"))
    state["198.51.100.9"] = {"expires_at": time.time() + 86400}
    S.STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setenv("MCP_REQUIRE_UNLOCK_TOKEN", "1")
    monkeypatch.setattr(S, "get_http_request", lambda: _Req(xff="198.51.100.9"))
    S.set_presented_token(token)
    assert S.require_unlocked() is not None


# ---- the default is record-only, and it records --------------------------------------

def test_enforcement_is_off_by_default(monkeypatch):
    monkeypatch.delenv("MCP_REQUIRE_UNLOCK_TOKEN", raising=False)
    assert S.enforce_unlock_token() is False


def test_a_call_without_a_token_is_allowed_but_counted(monkeypatch):
    """The counter is what lets the switch be flipped on evidence rather than on hope.

    Turning enforcement on while live callers still have no token is an outage, and an outage
    is how the whole change gets reverted. When this stops growing it is safe.
    """
    _unlock_with(monkeypatch)
    monkeypatch.delenv("MCP_REQUIRE_UNLOCK_TOKEN", raising=False)
    S.clear_presented_token()
    assert S.require_unlocked() is None, "the default must not break a live session"
    gap = lock_state.token_gap()
    assert gap.get("count") == 1
    assert IP in (gap.get("ips") or {})


def test_a_call_with_a_token_is_not_counted(monkeypatch):
    out = _unlock_with(monkeypatch)
    token = [l.split(": ", 1)[1] for l in out.splitlines() if l.startswith("unlock_token: ")][0]
    monkeypatch.delenv("MCP_REQUIRE_UNLOCK_TOKEN", raising=False)
    S.set_presented_token(token)
    assert S.require_unlocked() is None
    assert lock_state.token_gap().get("count", 0) == 0


# ---- the token must not outlive its call -------------------------------------------------

def test_the_presented_token_is_cleared_between_calls():
    """A token left behind authorises the NEXT call -- the same defect as the identity."""
    S.set_presented_token("abc")
    assert S.presented_token() == "abc"
    S.clear_presented_token()
    assert S.presented_token() == ""


def test_the_gateway_strips_the_token_before_dispatch():
    """The tool has no such parameter and would raise TypeError; and a credential should not
    be handed to arbitrary tool code by accident."""
    import inspect

    import main
    src = inspect.getsource(main)
    assert '_args.pop("unlock_token"' in src
    assert "fn(**_args)" in src, "the popped dict must be the one dispatched"
