"""SEC-20 regression tests for main.py's /health route.

Background: /health is registered via @mcp.custom_route, which sits OUTSIDE FastMCP's
StaticTokenVerifier auth (that guard only wraps the /mcp mount) -- so /health had no
authentication of its own and, reachable through the dev tunnel, handed server identity,
code-staleness, auth-failure counts and tool/fleet probe state to any caller on the public
URL with zero credentials. The fix (see main.py's health() / _health_bearer_ok()) gates the
full payload behind "genuine local peer" OR "presents this server's own API key as a bearer
token", using tools.security.derive_identity -- the SAME helper the unlock gate and the
auth-failure ASGI observer already use, so "local" means exactly what it means everywhere
else in this server. An unauthenticated, non-local caller still gets {"status": "ok",
"server_pid": ...} -- see main.py's health() docstring for why server_pid specifically stays
in that minimal reply (doctor.ps1 / status.py / FleetCockpit.cs already depend on it, sent
with no bearer, for a tunnel-mismatch check).

No real HTTP server or TestClient is spun up: fastmcp.server.server.custom_route's decorator
returns the wrapped function UNCHANGED (verified by reading its source), so main.health IS
the exact object FastMCP registers for GET /health, and calling it directly with a minimal
fake Request exercises the real route handler. The fake-Request approach mirrors
tests/test_security_xff.py's `_make_req` helper -- the existing convention in this repo for
exercising forwarding-header logic without an actual socket.

Run: .venv\\Scripts\\python.exe -m pytest -q tests\\test_health_route.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# main.py reads MCP_API_KEY at import time (`API_KEY = os.environ["MCP_API_KEY"]`), and this
# module is the first thing in the test session to `import main`. python-dotenv's
# load_dotenv() (also called at main.py import time) defaults to override=False, so setting
# this BEFORE the import wins over whatever the real .env on this machine has -- these tests
# then know the exact bearer value to assert against, without depending on (or ever printing)
# the real operator API key.
TEST_API_KEY = "sec-20-test-bearer-key-do-not-use"
os.environ.setdefault("MCP_API_KEY", TEST_API_KEY)

import main  # noqa: E402  (must follow the environment setup above)


def _make_request(peer_host: str, xff: str = "", authorization: str = "") -> MagicMock:
    """Minimal fake Starlette Request carrying only what main.health() reads:
    request.client.host and request.headers.get(...)."""
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = peer_host
    headers: dict[str, str] = {}
    if xff:
        headers["x-forwarded-for"] = xff
    if authorization:
        headers["authorization"] = authorization
    req.headers = MagicMock()
    req.headers.get = lambda key, default="": headers.get(key.lower(), default)
    return req


def _call_health(req) -> dict:
    resp = asyncio.run(main.health(req))
    return json.loads(resp.body)


# Fields that only ever appear in the full, local-or-bearer-authenticated payload. Checking
# for these (rather than the whole shape, which tool/fleet probe summaries are free to grow)
# is what tells "got the full thing" apart from "got the minimal thing".
_FULL_ONLY_MARKERS = ("server_code", "server_head", "server_uptime_s", "auth_fail_10m")


def test_genuine_local_peer_gets_full_payload():
    body = _call_health(_make_request(peer_host="127.0.0.1"))
    assert body["status"] == "ok"
    assert body.get("server_pid") == os.getpid()
    for key in _FULL_ONLY_MARKERS:
        assert key in body, f"local caller should get {key!r}, payload was {body!r}"


def test_genuine_local_peer_ipv6_gets_full_payload():
    body = _call_health(_make_request(peer_host="::1"))
    for key in _FULL_ONLY_MARKERS:
        assert key in body


def test_tunnel_forwarded_caller_with_no_bearer_gets_minimal_payload_only():
    """The exact SEC-20 scenario: the tunnel terminates on this machine so the raw TCP peer
    looks local, but an X-Forwarded-For header is present -- a real internet caller through
    the dev tunnel -- and no bearer token is presented. Must get nothing beyond status and
    server_pid."""
    body = _call_health(_make_request(peer_host="127.0.0.1", xff="203.0.113.7"))
    assert body == {"status": "ok", "server_pid": os.getpid()}


def test_direct_remote_peer_with_no_bearer_gets_minimal_payload_only():
    body = _call_health(_make_request(peer_host="203.0.113.7"))
    assert body == {"status": "ok", "server_pid": os.getpid()}


def test_forwarded_caller_with_correct_bearer_gets_full_payload(monkeypatch):
    """Sets main.API_KEY explicitly for this test rather than trusting a key snapshotted at
    collection time (`_ACTUAL_KEY` used to be captured once, at `import main` above, but
    other test files in the same pytest session reload main.py under a synthetic
    MCP_API_KEY and don't all restore it before this file's tests run -- CI, 2026-09-24: the
    live main.API_KEY at call time had drifted from the collection-time snapshot, so the
    correct bearer stopped matching. monkeypatch.setattr pins what main compares against AND
    what this test sends to the same value, for the duration of this test only, so the
    assertion no longer depends on suite order at all.)."""
    monkeypatch.setattr(main, "API_KEY", TEST_API_KEY)
    body = _call_health(_make_request(
        peer_host="127.0.0.1", xff="203.0.113.7", authorization=f"Bearer {TEST_API_KEY}"))
    for key in _FULL_ONLY_MARKERS:
        assert key in body


def test_forwarded_caller_with_raw_unprefixed_key_gets_full_payload(monkeypatch):
    """Tolerates a raw (no "Bearer " scheme) Authorization value the same way
    main.py's _BearerPrefix does for /mcp -- an operator who pastes the raw API key should
    not see a silently-downgraded /health response for missing a word.

    Same order-independence fix as the test above: main.API_KEY is set explicitly for this
    test via monkeypatch rather than read from a collection-time snapshot."""
    monkeypatch.setattr(main, "API_KEY", TEST_API_KEY)
    body = _call_health(_make_request(
        peer_host="127.0.0.1", xff="203.0.113.7", authorization=TEST_API_KEY))
    for key in _FULL_ONLY_MARKERS:
        assert key in body


def test_forwarded_caller_with_wrong_bearer_gets_minimal_payload_only():
    body = _call_health(_make_request(
        peer_host="127.0.0.1", xff="203.0.113.7", authorization="Bearer not-the-real-key"))
    assert body == {"status": "ok", "server_pid": os.getpid()}


def test_spoofed_xff_claiming_loopback_is_not_treated_as_local():
    """Mirrors tests/test_security_xff.py's XFF-spoofing case: an XFF value of 127.0.0.1
    must NOT make a proxied/tunnelled request read as genuine-local -- derive_identity()
    requires a loopback PEER *and* the absence of any XFF header, and this request has one."""
    body = _call_health(_make_request(peer_host="127.0.0.1", xff="127.0.0.1"))
    assert body == {"status": "ok", "server_pid": os.getpid()}


def test_minimal_payload_never_grows_extra_keys():
    """Pins the exact shape of the unauthenticated reply -- a field added to the full
    payload in future must not accidentally leak into this branch too."""
    body = _call_health(_make_request(peer_host="198.51.100.1"))
    assert set(body.keys()) == {"status", "server_pid"}


def test_status_is_always_ok_regardless_of_auth():
    for req in (
        _make_request(peer_host="127.0.0.1"),
        _make_request(peer_host="198.51.100.1"),
    ):
        assert _call_health(req)["status"] == "ok"
