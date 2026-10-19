# -*- coding: utf-8 -*-
"""A private copy of the local MCP server for the preflight A/B, on port 8100.

Why it exists: main.py hard-codes port 8000 in its `__main__` block, and 8000 is the production
server. This launcher imports `main` as a module (so that block does NOT run) and serves the
same app on 8100. It reads MCP_API_KEY from the environment and refuses to start without one,
so it never needs a .env file and never reads the production one.

usage: python -I -X utf8 scripts/probes/preflight_ab_server.py [port]
Refuses 8000, 8765 and 9222-9224.
"""
import os
import sys

FORBIDDEN_PORTS = (8000, 8765, 9222, 9223, 9224)


def pick_port(argv):
    port = int(argv[1]) if len(argv) > 1 else 8100
    if port in FORBIDDEN_PORTS:
        raise SystemExit("refused: port %d belongs to production" % port)
    return port


def main(argv):
    port = pick_port(argv)
    if not (os.environ.get("MCP_API_KEY") or "").strip():
        raise SystemExit("refused: set MCP_API_KEY for this private server first")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.chdir(root)
    sys.path.insert(0, root)
    try:
        import psutil
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass
    import main as product            # does not run the __main__ block
    import uvicorn
    from starlette.middleware import Middleware

    class _AcceptBoth:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope.get("type") == "http":
                hdrs = [(k, v) for (k, v) in (scope.get("headers") or []) if k.lower() != b"accept"]
                hdrs.append((b"accept", b"application/json, text/event-stream"))
                scope = dict(scope)
                scope["headers"] = hdrs
            await self.app(scope, receive, send)

    app = product.mcp.http_app(path="/mcp", transport="streamable-http",
                               json_response=True, middleware=[Middleware(_AcceptBoth)])
    app = product._BearerPrefix(app)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main(sys.argv)
