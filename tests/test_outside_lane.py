import asyncio
import time
from types import SimpleNamespace

import anyio.to_thread
import pytest

from tools import registry
from tools.code_exec import _guard_waits, shell_exec


@pytest.mark.parametrize("command, expected", [
    ('cmd /c "ping -n 180 127.0.0.1 >nul & gh pr checks 487 --repo X ..."',
     'cmd /c "ping -n 20 127.0.0.1 >nul & gh pr checks 487 --repo X ..."'),
    ('cmd /c "timeout /t 240 /nobreak >nul 2>&1 & gh pr checks 430 2>&1 | tail -40"',
     'cmd /c "timeout /t 20 /nobreak >nul 2>&1 & gh pr checks 430 2>&1 | tail -40"'),
    ('cmd /c "timeout /t 150 /nobreak >nul 2>&1 & gh run view 425... --json status,conclusion"',
     'cmd /c "timeout /t 20 /nobreak >nul 2>&1 & gh run view 425... --json status,conclusion"'),
    ('sleep 30 && Start-Sleep -Seconds 45 & ping -n 21 localhost',
     'sleep 20 && Start-Sleep -Seconds 20 & ping -n 20 localhost'),
])
def test_compound_waits_are_shortened(command, expected, monkeypatch):
    assert _guard_waits(command) == (expected, True, None)
    monkeypatch.setattr("tools.code_exec.require_unlocked", lambda: None)
    monkeypatch.setattr("tools.code_exec._judged", lambda *args: None)
    monkeypatch.setattr("tools.code_exec._run_with_tree_timeout", lambda cmd, *args: cmd)
    assert shell_exec(command) == "[wait shortened to 20 s: poll again in a later call]\n" + expected


@pytest.mark.parametrize("command", [
    'cmd /c "cd /d <dir> && for /l %i in (1,1,40) do (... ping -n 21 127.0.0.1 ...)"',
    'gh run watch <id> --repo X --exit-status',
    'gh pr checks 250 --watch --interval 30',
])
def test_unbounded_polling_is_refused_with_alternatives(command, monkeypatch):
    monkeypatch.setattr("tools.code_exec.require_unlocked", lambda: None)
    result = shell_exec(command)
    assert "refused" in result
    assert "gh run view <id> --json status,conclusion" in result
    assert "gh pr checks <n> without --watch" in result


def test_plain_command_untouched():
    assert _guard_waits("git status") == ("git status", False, None)
    assert _guard_waits("for /l %i in (1,1,3) do (sleep 20)") == ("for /l %i in (1,1,3) do (sleep 20)", False, None)
    assert _guard_waits("for /l %i in (1,1,40) do (echo ok) & sleep 30") == (
        "for /l %i in (1,1,40) do (echo ok) & sleep 20", True, None)


def test_peer_lane_with_forwarded_header(monkeypatch):
    import fastmcp.server.dependencies as dependencies
    request = SimpleNamespace(scope={"client": ("127.0.0.1", 123), "headers": []})
    monkeypatch.setattr(dependencies, "get_http_request", lambda: request)
    monkeypatch.delenv("MCP_OUTSIDE_LANE", raising=False)
    assert not registry._outside_peer()  # fleet sends no forwarding header
    # Forging the header only selects the bounded lane and grants nothing; the
    # tunnel adds it, so callers on that path cannot suppress it.
    request.scope["headers"] = [(b"x-forwarded-for", b"203.0.113.1")]
    assert registry._outside_peer()
    request.scope["headers"] = []
    request.scope["client"] = ("203.0.113.1", 123)
    assert registry._outside_peer()
    monkeypatch.setenv("MCP_OUTSIDE_LANE", "0")
    assert not registry._outside_peer()


def test_outside_call_completes_when_fleet_limiter_is_full(monkeypatch):
    import fastmcp.server.dependencies as dependencies
    request = SimpleNamespace(scope={"client": ("127.0.0.1", 123),
                                     "headers": [(b"x-forwarded-for", b"203.0.113.1")]})
    monkeypatch.setattr(dependencies, "get_http_request", lambda: request)
    monkeypatch.delenv("MCP_OUTSIDE_LANE", raising=False)

    async def exercise():
        limiter = anyio.to_thread.current_default_thread_limiter()
        old = limiter.total_tokens
        limiter.total_tokens = 40
        borrowers = [object() for _ in range(40)]
        try:
            for borrower in borrowers:
                limiter.acquire_on_behalf_of_nowait(borrower)
            start = time.monotonic()
            assert await asyncio.wait_for(registry._to_async(lambda: "PONG")(), 1) == "PONG"
            assert time.monotonic() - start < 1
        finally:
            for borrower in borrowers:
                limiter.release_on_behalf_of(borrower)
            limiter.total_tokens = old

    asyncio.run(exercise())
