"""Local (non-HTTP) per-IP unlock admin: tools.security.grant_ip / revoke_ip / list_grants.

These are cockpit-only helpers (see the long header comment above them in tools/security.py)
-- never registered as MCP tools, never touching require_unlocked()/unlock()/_parse_request().
This file locks that boundary down: the admin surface behaves correctly in isolation, AND
require_unlocked()'s own behaviour (already covered exhaustively by tests/test_security_xff.py)
is provably unaffected by using it -- a grant made here is honoured by the real gate, and a
revoke here re-locks it, without grant_ip/revoke_ip calling or patching require_unlocked at all.

Run: pytest -q tools/test_security.py
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools import security as sec


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


# ── grant_ip: creates with the right TTL ────────────────────────────────────────

def test_grant_creates_with_default_ttl(monkeypatch):
    monkeypatch.delenv("MCP_UNLOCK_TTL_DAYS", raising=False)
    before = time.time()
    result = sec.grant_ip("203.0.113.5")
    assert result["ip"] == "203.0.113.5"
    assert result["ttl_days"] == 30.0
    assert abs(result["expires_at"] - (before + 30 * 86400)) < 5

    state = json.loads(sec.STATE_FILE.read_text(encoding="utf-8"))
    assert "203.0.113.5" in state


def test_grant_respects_ttl_env_override(monkeypatch):
    monkeypatch.setenv("MCP_UNLOCK_TTL_DAYS", "7")
    result = sec.grant_ip("203.0.113.6")
    assert result["ttl_days"] == 7.0


def test_grant_explicit_ttl_overrides_env(monkeypatch):
    monkeypatch.setenv("MCP_UNLOCK_TTL_DAYS", "7")
    result = sec.grant_ip("203.0.113.7", ttl_days=1)
    assert result["ttl_days"] == 1.0


def test_grant_accepts_ipv6():
    result = sec.grant_ip("2001:db8::1")
    assert result["ip"] == "2001:db8::1"


# ── grant on an existing IP extends ─────────────────────────────────────────────

def test_grant_on_existing_ip_extends_not_duplicates():
    first = sec.grant_ip("203.0.113.8", ttl_days=1)
    second = sec.grant_ip("203.0.113.8", ttl_days=30)
    assert second["expires_at"] > first["expires_at"]
    state = json.loads(sec.STATE_FILE.read_text(encoding="utf-8"))
    assert len(state) == 1


# ── revoke removes ───────────────────────────────────────────────────────────────

def test_revoke_removes_entry():
    sec.grant_ip("203.0.113.9")
    assert sec.revoke_ip("203.0.113.9") is True
    state = json.loads(sec.STATE_FILE.read_text(encoding="utf-8"))
    assert "203.0.113.9" not in state


def test_revoke_missing_ip_is_a_noop_not_an_error():
    assert sec.revoke_ip("203.0.113.10") is False


def test_revoke_invalid_ip_is_refused():
    with pytest.raises(ValueError):
        sec.revoke_ip("garbage")


# ── expired entries report as expired ───────────────────────────────────────────

def test_expired_entry_reports_as_expired():
    sec.grant_ip("203.0.113.11", ttl_days=-1)   # already in the past
    grants = sec.list_grants()
    entry = next(g for g in grants if g["ip"] == "203.0.113.11")
    assert entry["expired"] is True
    assert entry["remaining_seconds"] < 0


def test_unexpired_entry_reports_not_expired():
    sec.grant_ip("203.0.113.12", ttl_days=30)
    grants = sec.list_grants()
    entry = next(g for g in grants if g["ip"] == "203.0.113.12")
    assert entry["expired"] is False
    assert entry["remaining_seconds"] > 0


# ── an invalid IP is refused ─────────────────────────────────────────────────────

def test_grant_invalid_ip_is_refused():
    for bad in ("", "   ", "not-an-ip", "999.999.999.999", "<script>"):
        with pytest.raises(ValueError):
            sec.grant_ip(bad)
    assert not sec.STATE_FILE.exists()


def test_valid_ip_accepts_v4_and_v6():
    assert sec._valid_ip("1.2.3.4")
    assert sec._valid_ip("2001:db8::1")


def test_valid_ip_rejects_garbage():
    for bad in ("", "   ", None, "not-an-ip", "1.2.3.4.5", "<script>alert(1)</script>"):
        assert sec._valid_ip(bad) is False


# ── the write is atomic ──────────────────────────────────────────────────────────

def test_save_state_atomic_never_leaves_a_tmp_file_behind():
    sec.grant_ip("203.0.113.13")
    assert list(sec.STATE_FILE.parent.glob("*.tmp")) == []


def test_save_state_atomic_goes_through_tempfile_plus_os_replace(monkeypatch):
    """Guards the concurrency property: the write must go through a temp file + os.replace,
    never a direct write to STATE_FILE, so the live server process (which reads/writes the
    same file on every unlock() call) can never observe a half-written file."""
    calls = []
    real_replace = os.replace

    def spy_replace(src, dst):
        calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)
    sec.grant_ip("203.0.113.14")
    assert len(calls) == 1
    src, dst = calls[0]
    assert str(src) != str(sec.STATE_FILE)
    assert str(src).endswith(".tmp")
    assert str(dst) == str(sec.STATE_FILE)


def test_concurrent_grants_do_not_corrupt_the_state_file():
    """_GRANT_LOCK serialises writers from this process; many threads granting at once must
    still leave valid, complete JSON with every entry present -- not a torn/partial file."""
    ips = ["10.0.0.%d" % i for i in range(1, 21)]
    threads = [threading.Thread(target=sec.grant_ip, args=(ip,)) for ip in ips]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    state = json.loads(sec.STATE_FILE.read_text(encoding="utf-8"))
    for ip in ips:
        assert ip in state


# ── require_unlocked() behaviour is UNCHANGED ───────────────────────────────────

def test_require_unlocked_still_denies_by_default():
    req = _make_req(peer_host="198.51.100.1", xff="")
    with patch("tools.security.get_http_request", return_value=req):
        assert sec.require_unlocked() is not None


def test_grant_ip_is_honoured_by_the_real_unlock_gate():
    """grant_ip and require_unlocked() read/write the exact same state file, so a grant made
    from the cockpit unlocks that IP through the ordinary gate -- without grant_ip calling,
    patching, or otherwise touching require_unlocked/unlock/_parse_request.

    THE TOKEN IS PRESENTED because grant_ip now issues one, and this then holds whether or not
    enforcement is on. Without it the test passed only while enforcement was off, which made
    it depend on the operator's .env: `main` loads it, so importing main anywhere in the suite
    put MCP_REQUIRE_UNLOCK_TOKEN into the process and these two tests began to fail locally
    while CI, which has no .env, stayed green. A test whose verdict depends on a file that is
    not in the repository is not testing the code.
    """
    granted = sec.grant_ip("198.51.100.2")
    req = _make_req(peer_host="127.0.0.1", xff="198.51.100.2")
    sec.set_presented_token(granted["unlock_token"])
    try:
        with patch("tools.security.get_http_request", return_value=req):
            assert sec.require_unlocked() is None
    finally:
        sec.clear_presented_token()


def test_a_granted_ip_without_its_token_is_refused_under_enforcement(monkeypatch):
    """The other half: the grant is necessary and, once enforcement is on, not sufficient."""
    sec.grant_ip("198.51.100.4")
    req = _make_req(peer_host="127.0.0.1", xff="198.51.100.4")
    monkeypatch.setenv("MCP_REQUIRE_UNLOCK_TOKEN", "1")
    sec.clear_presented_token()
    with patch("tools.security.get_http_request", return_value=req):
        assert sec.require_unlocked() is not None


def test_revoke_ip_re_locks_the_real_unlock_gate():
    granted = sec.grant_ip("198.51.100.3")
    req = _make_req(peer_host="127.0.0.1", xff="198.51.100.3")
    sec.set_presented_token(granted["unlock_token"])
    try:
        with patch("tools.security.get_http_request", return_value=req):
            assert sec.require_unlocked() is None
        sec.revoke_ip("198.51.100.3")
        with patch("tools.security.get_http_request", return_value=req):
            assert sec.require_unlocked() is not None
    finally:
        sec.clear_presented_token()


def test_grant_ip_and_revoke_ip_are_not_registered_as_mcp_tools():
    """Header comment's core guarantee: main.py must never import/register grant_ip/revoke_ip
    as MCP tools. Reads the source as text rather than importing main.py, so this test does not
    pay for -- or risk -- main.py's own import-time side effects (FastMCP app + full tool
    registration)."""
    main_src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    assert "grant_ip" not in main_src
    assert "revoke_ip" not in main_src
    assert "from tools.security import list_unlocked, unlock" in main_src


# ── CLI surface (what the cockpit shells out to) ────────────────────────────────

def test_cli_list_grant_revoke_roundtrip(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["security.py", "grant", "203.0.113.20", "--ttl-days", "5"])
    sec._cli()
    granted = json.loads(capsys.readouterr().out)
    assert granted["ip"] == "203.0.113.20"
    assert granted["ttl_days"] == 5.0

    monkeypatch.setattr("sys.argv", ["security.py", "list"])
    sec._cli()
    listed = json.loads(capsys.readouterr().out)
    assert any(g["ip"] == "203.0.113.20" for g in listed)

    monkeypatch.setattr("sys.argv", ["security.py", "revoke", "203.0.113.20"])
    sec._cli()
    revoked = json.loads(capsys.readouterr().out)
    assert revoked == {"ip": "203.0.113.20", "revoked": True}


def test_cli_grant_invalid_ip_exits_nonzero_with_error_json(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["security.py", "grant", "not-an-ip"])
    with pytest.raises(SystemExit) as exc:
        sec._cli()
    assert exc.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert "error" in out


# ── 発行しすぎたときの、宣言されていない取り消し ──────────────────────────────

def test_granting_again_past_the_cap_silently_retires_the_oldest_token(monkeypatch):
    """同じ相手に何度も発行すると、古いトークンは黙って捨てられる。

    これは事実上の取り消しでありながら、どこにもそう書かれていない。捨てられた側は
    「許可されているはず」の顔をしたまま拒否されるし、逆にもし捨てられていなければ、
    運用者が把握していない鍵が無期限に生き残ることになる。どちらであるかは
    推測ではなくテストで固定しておくべき性質。

    ここでは前者(捨てられる)であることを確かめる。上限を超えて発行し直したら、
    最初のトークンはもう通らない。"""
    # トークンの照合はこの旗が立っている時だけ行われる。立てずに書くと、
    # 「どのトークンでも通る」状態を「捨てられていない」と読み違える。
    monkeypatch.setenv("MCP_REQUIRE_UNLOCK_TOKEN", "1")
    ip = "198.51.100.77"
    cap = sec._MAX_TOKENS_PER_IDENTITY
    first = sec.grant_ip(ip)["unlock_token"]
    for _ in range(cap):
        newest = sec.grant_ip(ip)["unlock_token"]

    req = _make_req(peer_host="127.0.0.1", xff=ip)
    try:
        sec.set_presented_token(first)
        with patch("tools.security.get_http_request", return_value=req):
            assert sec.require_unlocked() is not None, (
                "上限を超えても最初のトークンが通っている -- "
                "運用者が知らない鍵が生き続けることになる")
        sec.set_presented_token(newest)
        with patch("tools.security.get_http_request", return_value=req):
            assert sec.require_unlocked() is None, "最新のトークンまで巻き添えで無効化している"
    finally:
        sec.clear_presented_token()
        sec.revoke_ip(ip)


def test_revocation_needs_no_restart_and_survives_no_cache(monkeypatch):
    """取り消しは即時に効くこと。

    状態はファイルから毎回読み直される設計だが、どこかが判定を握った瞬間に
    取り消しは再起動まで効かなくなる。「効いているはず」を仕様として固定する。"""
    monkeypatch.setenv("MCP_REQUIRE_UNLOCK_TOKEN", "1")
    ip = "198.51.100.78"
    granted = sec.grant_ip(ip)
    req = _make_req(peer_host="127.0.0.1", xff=ip)
    try:
        sec.set_presented_token(granted["unlock_token"])
        with patch("tools.security.get_http_request", return_value=req):
            assert sec.require_unlocked() is None
            # 同じプロセス・同じ contextvar・同じ patch のまま取り消す
            sec.revoke_ip(ip)
            assert sec.require_unlocked() is not None, (
                "取り消し後も同じ呼び出しが通る -- どこかが判定を握っている")
    finally:
        sec.clear_presented_token()
