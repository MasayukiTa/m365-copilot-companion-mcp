import asyncio
import time
from types import SimpleNamespace

import anyio.to_thread

from tools import registry


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
