"""doctor's own negative auth probe must not raise the "key mismatch" alarm.

THE DEFECT. scripts/doctor.ps1 section 6 POSTs /mcp WITHOUT a bearer on purpose, to prove auth
is enforced at all. main.py's outermost ASGI layer (_BearerPrefix) counted that 401 as an auth
failure, so every doctor run -- and the cockpit's auto-repair runs doctor repeatedly -- raised
auth_fail_10m (measured 14-16 on 2026-09-24) and the cockpit's server dot went amber with
"suspect MCP_API_KEY mismatch", with no client misconfigured at all.

THE FIX. A request that is loopback with no forwarding headers (tools.security.derive_identity,
plus no other Forwarded / X-Forwarded-* header) AND carries X-MCP-Self-Test is counted apart, as
self_test_rejections_10m. A request the tunnel forwards (it carries X-Forwarded-For) still counts
as a real rejection even when it carries the marker.

HOW THIS RUNS. The real FastMCP app (main.mcp.http_app, the same call main.py's __main__ makes)
wrapped in the real main._BearerPrefix, driven by Starlette's TestClient with the client address
set per request -- no socket, no uvicorn, nothing on :8000. The auth-stats singleton and its two
files are swapped for fresh ones so nothing reaches the operator's .fleet/.

Run: .venv\\Scripts\\python.exe -m pytest -p no:pytest-bdd -q tests\\test_doctor_self_test_is_not_a_key_mismatch.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("MCP_API_KEY", "self-test-probe-key-do-not-use")

import main  # noqa: E402
from tools import auth_stats  # noqa: E402

from starlette.testclient import TestClient  # noqa: E402

_BODY = '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'


@pytest.fixture()
def counters(monkeypatch, tmp_path):
    monkeypatch.setattr(auth_stats, "_TRACKER", auth_stats.AuthFailureTracker())
    monkeypatch.setattr(auth_stats, "_STATS_FILE", tmp_path / "auth_stats.json")
    monkeypatch.setattr(auth_stats, "_REJECTIONS_FILE", tmp_path / "auth_rejections.jsonl")
    monkeypatch.setattr(main, "_SELF_TEST_TRACKER", None)
    return tmp_path


def _post(peer: str, headers: dict) -> int:
    app = main._BearerPrefix(main.mcp.http_app(path="/mcp", transport="streamable-http",
                                               json_response=True))
    with TestClient(app, client=(peer, 50123), raise_server_exceptions=False) as c:
        r = c.post("/mcp", content=_BODY,
                   headers=dict({"content-type": "application/json",
                                 "accept": "application/json, text/event-stream"}, **headers))
    return r.status_code


def _mismatch() -> int:
    return auth_stats.get_summary()["auth_fail_10m"]


def _self_test() -> int:
    return main._self_test_summary()["self_test_rejections_10m"]


def test_doctors_marked_loopback_probe_is_refused_but_not_counted_as_a_mismatch(counters):
    status = _post("127.0.0.1", {"X-MCP-Self-Test": "doctor"})
    assert status == 401, "the negative probe must still be REFUSED -- auth is what it proves"
    assert _mismatch() == 0, "doctor's own probe raised the key-mismatch counter"
    assert _self_test() == 1, "the self-test rejection was not counted at all"
    assert not (counters / "auth_rejections.jsonl").exists(), \
        "doctor's probe was written into the record of callers turned away"


def test_an_unmarked_loopback_rejection_still_counts(counters):
    assert _post("127.0.0.1", {}) == 401
    assert _mismatch() == 1 and _self_test() == 0


@pytest.mark.parametrize("fwd", [
    {"X-Forwarded-For": "20.210.1.2"},        # what the dev tunnel host adds
    {"X-Forwarded-For": "127.0.0.1"},         # a forwarded request claiming to be local
    {"X-Forwarded-Host": "example.devtunnels.ms"},
    {"Forwarded": "for=20.210.1.2"},
])
def test_a_forwarded_request_carrying_the_marker_still_counts(counters, fwd):
    headers = dict({"X-MCP-Self-Test": "doctor"}, **fwd)
    assert _post("127.0.0.1", headers) == 401
    assert _mismatch() == 1, "a forwarded request hid a real rejection behind the marker (%r)" % fwd
    assert _self_test() == 0


def test_a_non_loopback_peer_carrying_the_marker_still_counts(counters):
    assert _post("10.1.2.3", {"X-MCP-Self-Test": "doctor"}) == 401
    assert _mismatch() == 1 and _self_test() == 0


def test_a_correct_key_is_neither(counters):
    status = _post("127.0.0.1", {"Authorization": "Bearer " + main.API_KEY,
                                 "X-MCP-Self-Test": "doctor"})
    assert status != 401
    assert _mismatch() == 0 and _self_test() == 0


def test_local_health_reports_the_self_test_count_apart(counters):
    _post("127.0.0.1", {"X-MCP-Self-Test": "doctor"})
    req = MagicMock()
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    req.headers = MagicMock()
    req.headers.get = lambda key, default="": default
    body = json.loads(asyncio.run(main.health(req)).body)
    assert body["auth_fail_10m"] == 0
    assert body["self_test_rejections_10m"] == 1


def test_doctor_sends_the_marker_on_its_negative_probe_only():
    doctor = (REPO / "scripts" / "doctor.ps1").read_text(encoding="utf-8")
    auth = doctor[doctor.index('Check "auth_bearer"'):]
    auth = auth[:auth.index("Write-Host")]
    assert "$noKey = Mcp-Status @{ 'X-MCP-Self-Test' = 'doctor' }" in auth
    with_key = [l for l in auth.splitlines() if "$withKey = Mcp-Status" in l]
    assert with_key and "X-MCP-Self-Test" not in with_key[0], \
        "the probe WITH the key must look like a real client"
