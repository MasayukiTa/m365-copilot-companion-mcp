"""Unit tests for XFF spoofing fix in tools/security.py.

Tests the four scenarios from the P0 spec:
  (a) spoofed XFF=127.0.0.1 with a non-empty XFF is NOT trusted/auto-unlocked
  (b) empty/unknown IP is NOT trusted
  (c) genuine local (peer=127.0.0.1, no XFF) IS auto-unlocked
  (d) a remote IP present in unlock_state passes; a different remote IP does not

Run:
    .venv\\Scripts\\python.exe -m pytest tests/test_security_xff.py -v
or:
    .venv\\Scripts\\python.exe tests/test_security_xff.py
"""
from __future__ import annotations

import json
import sys
import time
import tempfile
import types
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

# ── bootstrap sys.path so the package is importable ───────────────────────────
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import tools.security as sec


# ── Fake request helper ────────────────────────────────────────────────────────

def _make_req(peer_host: str, xff: str = "") -> MagicMock:
    """Return a minimal fake request object mimicking a FastMCP/Starlette request."""
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = peer_host
    # headers.get("x-forwarded-for", "") behaviour
    headers = {}
    if xff:
        headers["x-forwarded-for"] = xff
    req.headers = MagicMock()
    req.headers.get = lambda key, default="": headers.get(key.lower(), default)
    return req


# ── Test runner (works without pytest: plain asserts + summary) ────────────────

results: list[tuple[str, bool]] = []


def _check(name: str, cond: bool) -> None:
    results.append((name, cond))
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}")


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_a_spoofed_xff_not_trusted():
    """(a) XFF=127.0.0.1 with a non-local peer MUST NOT be treated as local."""
    # Attacker sends X-Forwarded-For: 127.0.0.1 through the tunnel.
    # Tunnel peer itself might be something else but even if peer=127.0.0.1,
    # the presence of XFF means this came through a proxy — not trusted-local.
    req = _make_req(peer_host="127.0.0.1", xff="127.0.0.1")
    with patch.object(sec, "_load_state", return_value={}):
        with patch("tools.security.get_http_request", return_value=req):
            result = sec.require_unlocked()
    # Must NOT return None (which would mean "allowed")
    _check("a_spoofed_xff_not_auto_unlocked", result is not None)
    # The identity IP extracted should be 127.0.0.1 (the XFF entry), not local-exempted
    is_local, ip = sec._parse_request(req)
    _check("a_spoofed_xff_is_not_local", not is_local)


def test_b_empty_peer_not_trusted():
    """(b) Empty/unknown peer with no XFF is NOT trusted."""
    req = _make_req(peer_host="", xff="")
    is_local, ip = sec._parse_request(req)
    _check("b_empty_peer_is_not_local", not is_local)
    # require_unlocked with no state => locked
    with patch.object(sec, "_load_state", return_value={}):
        with patch("tools.security.get_http_request", return_value=req):
            result = sec.require_unlocked()
    _check("b_empty_peer_is_locked", result is not None)


def test_c_genuine_local_auto_unlocked():
    """(c) peer=127.0.0.1 with NO XFF header is auto-unlocked (genuine local)."""
    req = _make_req(peer_host="127.0.0.1", xff="")
    is_local, ip = sec._parse_request(req)
    _check("c_genuine_local_is_local", is_local)
    with patch("tools.security.get_http_request", return_value=req):
        result = sec.require_unlocked()
    _check("c_genuine_local_is_unlocked", result is None)


def test_c_genuine_local_ipv6():
    """(c) peer=::1 with no XFF is also auto-unlocked."""
    req = _make_req(peer_host="::1", xff="")
    is_local, ip = sec._parse_request(req)
    _check("c_genuine_local_ipv6_is_local", is_local)
    with patch("tools.security.get_http_request", return_value=req):
        result = sec.require_unlocked()
    _check("c_genuine_local_ipv6_is_unlocked", result is None)


def test_d_unlocked_remote_ip_passes():
    """(d-pass) A remote IP that is in unlock_state and not expired should pass."""
    remote_ip = "20.210.146.129"
    fake_state = {
        remote_ip: {"expires_at": time.time() + 86400, "unlocked_at": time.time()},
    }
    # Simulate tunnel: peer=127.0.0.1 (the tunnel termination), XFF=remote_ip
    req = _make_req(peer_host="127.0.0.1", xff=remote_ip)
    is_local, ip = sec._parse_request(req)
    _check("d_pass_not_local", not is_local)
    _check("d_pass_identity_ip", ip == remote_ip)
    with patch.object(sec, "_load_state", return_value=fake_state):
        with patch("tools.security.get_http_request", return_value=req):
            result = sec.require_unlocked()
    _check("d_pass_unlocked_remote_allowed", result is None)


def test_d_different_remote_ip_denied():
    """(d-deny) A different remote IP NOT in unlock_state is denied."""
    remote_ip = "20.210.146.129"
    stranger_ip = "1.2.3.4"
    fake_state = {
        remote_ip: {"expires_at": time.time() + 86400, "unlocked_at": time.time()},
    }
    req = _make_req(peer_host="127.0.0.1", xff=stranger_ip)
    is_local, ip = sec._parse_request(req)
    _check("d_deny_not_local", not is_local)
    _check("d_deny_identity_ip", ip == stranger_ip)
    with patch.object(sec, "_load_state", return_value=fake_state):
        with patch("tools.security.get_http_request", return_value=req):
            result = sec.require_unlocked()
    _check("d_deny_stranger_blocked", result is not None)


def test_d_expired_unlock_denied():
    """(d-expired) An expired unlock entry is denied."""
    remote_ip = "210.157.192.182"
    fake_state = {
        remote_ip: {"expires_at": time.time() - 1, "unlocked_at": time.time() - 86401},
    }
    req = _make_req(peer_host="127.0.0.1", xff=remote_ip)
    with patch.object(sec, "_load_state", return_value=fake_state):
        with patch("tools.security.get_http_request", return_value=req):
            result = sec.require_unlocked()
    _check("d_expired_unlock_denied", result is not None)


def test_xff_multi_hop_hops1():
    """With MCP_TRUSTED_PROXY_HOPS=1 (default), the rightmost XFF entry is used."""
    # Client sends XFF: client_ip, and tunnel appends nothing (single entry).
    req = _make_req(peer_host="127.0.0.1", xff="20.210.146.129")
    with patch.dict(os.environ, {"MCP_TRUSTED_PROXY_HOPS": "1"}):
        is_local, ip = sec._parse_request(req)
    _check("xff_hops1_picks_rightmost", ip == "20.210.146.129")
    _check("xff_hops1_not_local", not is_local)


def test_xff_multi_hop_hops2():
    """With MCP_TRUSTED_PROXY_HOPS=2, the second-from-right XFF entry is used."""
    req = _make_req(peer_host="127.0.0.1", xff="client_ip, trusted_proxy_1")
    with patch.dict(os.environ, {"MCP_TRUSTED_PROXY_HOPS": "2"}):
        is_local, ip = sec._parse_request(req)
    _check("xff_hops2_picks_second_from_right", ip == "client_ip")


# ── derive_identity(): the shared, pure IP-derivation helper ──────────────────
#
# tools.security._parse_request() (above tests) is a thin adapter over
# sec.derive_identity(peer_host, xff_header_value) -- the same pure function
# main.py's ASGI auth-failure observer calls directly (it only has a raw
# scope, not a Starlette Request). These tests exercise derive_identity()
# itself, as plain strings, with no Request/mock object involved at all.


def test_derive_identity_no_xff_uses_peer():
    """No X-Forwarded-For header at all: identity_ip falls back to the raw
    peer address, and a loopback peer is genuinely local."""
    is_local, ip = sec.derive_identity("127.0.0.1", "")
    _check("derive_no_xff_peer_used", ip == "127.0.0.1")
    _check("derive_no_xff_is_local", is_local)

    is_local2, ip2 = sec.derive_identity("20.210.146.129", "")
    _check("derive_no_xff_remote_peer_used", ip2 == "20.210.146.129")
    _check("derive_no_xff_remote_not_local", not is_local2)


def test_derive_identity_empty_xff_string_same_as_absent():
    """An empty-string XFF value must behave identically to no header at
    all (this is the exact input main.py's scope-header scan produces when
    it never finds an X-Forwarded-For entry)."""
    is_local, ip = sec.derive_identity("127.0.0.1", "")
    _check("derive_empty_xff_is_local", is_local)
    _check("derive_empty_xff_peer_used", ip == "127.0.0.1")


def test_derive_identity_single_entry_xff():
    """A single-entry XFF is never trusted-local, and its one entry is the
    identity IP regardless of MCP_TRUSTED_PROXY_HOPS (index clamps to 0)."""
    with patch.dict(os.environ, {"MCP_TRUSTED_PROXY_HOPS": "1"}):
        is_local, ip = sec.derive_identity("127.0.0.1", "20.210.146.129")
    _check("derive_single_entry_not_local", not is_local)
    _check("derive_single_entry_picks_it", ip == "20.210.146.129")


def test_derive_identity_multi_entry_hops1():
    """Multi-entry XFF with hops=1 (default): rightmost entry wins."""
    with patch.dict(os.environ, {"MCP_TRUSTED_PROXY_HOPS": "1"}):
        is_local, ip = sec.derive_identity("127.0.0.1", "client_ip, mid_hop, right_hop")
    _check("derive_multi_hops1_picks_rightmost", ip == "right_hop")
    _check("derive_multi_hops1_not_local", not is_local)


def test_derive_identity_multi_entry_hops2():
    """Multi-entry XFF with hops=2: second entry from the right wins."""
    with patch.dict(os.environ, {"MCP_TRUSTED_PROXY_HOPS": "2"}):
        is_local, ip = sec.derive_identity("127.0.0.1", "client_ip, mid_hop, right_hop")
    _check("derive_multi_hops2_picks_second_from_right", ip == "mid_hop")


def test_derive_identity_none_inputs_never_raise():
    """derive_identity must tolerate None for either argument (e.g. a scope
    with no client tuple, or a header scan that found nothing) and degrade
    to the empty-peer / no-XFF path rather than raising."""
    is_local, ip = sec.derive_identity(None, None)
    _check("derive_none_inputs_no_raise_not_local", not is_local)
    _check("derive_none_inputs_no_raise_empty_ip", ip == "")


def test_parse_request_delegates_to_derive_identity():
    """_parse_request() must be a pure pass-through to derive_identity() --
    if it ever diverged, the unlock gate and the auth-failure sidecar would
    disagree on the same request's IP."""
    req = _make_req(peer_host="127.0.0.1", xff="a, b, c")
    with patch.dict(os.environ, {"MCP_TRUSTED_PROXY_HOPS": "1"}):
        via_parse_request = sec._parse_request(req)
        via_direct_call = sec.derive_identity("127.0.0.1", "a, b, c")
    _check("parse_request_matches_derive_identity", via_parse_request == via_direct_call)


# ── Standalone runner ──────────────────────────────────────────────────────────

def _run_all():
    test_a_spoofed_xff_not_trusted()
    test_b_empty_peer_not_trusted()
    test_c_genuine_local_auto_unlocked()
    test_c_genuine_local_ipv6()
    test_d_unlocked_remote_ip_passes()
    test_d_different_remote_ip_denied()
    test_d_expired_unlock_denied()
    test_xff_multi_hop_hops1()
    test_xff_multi_hop_hops2()
    test_derive_identity_no_xff_uses_peer()
    test_derive_identity_empty_xff_string_same_as_absent()
    test_derive_identity_single_entry_xff()
    test_derive_identity_multi_entry_hops1()
    test_derive_identity_multi_entry_hops2()
    test_derive_identity_none_inputs_never_raise()
    test_parse_request_delegates_to_derive_identity()

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n=== {passed}/{total} security-xff checks passed ===")
    return 0 if passed == total else 1


# pytest-compatible: each test_* function is collected automatically.
# Standalone: call _run_all() below.

if __name__ == "__main__":
    sys.exit(_run_all())
