import asyncio
import threading
import time
from types import SimpleNamespace

import anyio.to_thread
import pytest

from tools import registry
from tools.code_exec import _wait_only, shell_exec


@pytest.mark.parametrize("command", [
    "ping -n 180 127.0.0.1", "ping -n 180 localhost", "timeout /t 240",
    "sleep 21", "Start-Sleep 21", "gh run watch", "gh pr checks --watch",
])
def test_wait_only_commands_are_refused(command, monkeypatch):
    monkeypatch.setattr("tools.code_exec.require_unlocked", lambda: None)
    assert _wait_only(command)
    assert "resumed later by the fleet" in shell_exec(command)


@pytest.mark.parametrize("command", [
    "sleep 20", "ping -n 20 localhost", "echo sleep 30", "sleep 30 && echo done",
    "ping -n 180 example.com", "gh pr checks", "gh run watch && echo done",
])
def test_other_commands_are_not_wait_only(command):
    assert not _wait_only(command)


def test_peer_lane_ignores_forwarded_headers(monkeypatch):
    import fastmcp.server.dependencies as dependencies
    request = SimpleNamespace(scope={"client": ("127.0.0.1", 123),
                                     "headers": [(b"x-forwarded-for", b"203.0.113.1")]})
    monkeypatch.setattr(dependencies, "get_http_request", lambda: request)
    monkeypatch.delenv("MCP_OUTSIDE_LANE", raising=False)
    assert not registry._outside_peer()
    request.scope["client"] = ("203.0.113.1", 123)
    assert registry._outside_peer()
    monkeypatch.setenv("MCP_OUTSIDE_LANE", "0")
    assert not registry._outside_peer()


def test_outside_call_completes_when_fleet_limiter_is_full(monkeypatch):
    import fastmcp.server.dependencies as dependencies
    request = SimpleNamespace(scope={"client": ("203.0.113.1", 123)})
    monkeypatch.setattr(dependencies, "get_http_request", lambda: request)
    monkeypatch.delenv("MCP_OUTSIDE_LANE", raising=False)

    async def exercise():
        limiter = anyio.to_thread.current_default_thread_limiter()
        old = limiter.total_tokens
        limiter.total_tokens = 1
        started, release = threading.Event(), threading.Event()

        def fleet():
            started.set()
            release.wait(5)

        task = asyncio.create_task(anyio.to_thread.run_sync(fleet))
        try:
            while not started.is_set():
                await asyncio.sleep(0.001)
            start = time.monotonic()
            assert await asyncio.wait_for(registry._to_async(lambda: "PONG")(), 1) == "PONG"
            assert time.monotonic() - start < 1
        finally:
            release.set()
            await task
            limiter.total_tokens = old

    asyncio.run(exercise())
