"""Real MCP handshake probe (initialize -> notifications/initialized -> tools/list).

This is the exact exchange the owner's chat client performs, which plain
``GET /health`` monitoring never reproduced. It is importable (the monitor in
``tools/mcp_handshake_monitor.py`` calls :func:`handshake`) and also runnable as a
script for a one-off probe.

It NEVER prints or returns the API key. The key is resolved the way the server
resolves it: load ``.env`` into the environment, then ask
``tools.secret_store`` for the bearer token (DPAPI-aware).

Adapted from the reference probe measured working on 2026-11-11 18:32 against both
the loopback server and the public devtunnel URL.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional


def load_env_file(env_path: str) -> None:
    """Load KEY=VALUE pairs from ``env_path`` into os.environ (setdefault).

    Mirrors the reference probe: skips blanks and ``#`` comments, strips a single
    layer of surrounding double quotes, and never overwrites a value already in the
    environment. Values are never logged.
    """
    try:
        with open(env_path, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))
    except OSError:
        # A missing .env is not fatal here: the environment may already carry the key.
        pass


def resolve_api_key(env_path: Optional[str] = None) -> str:
    """Resolve the bearer token the same way the server does. Never returns None.

    If ``env_path`` is given it is loaded first (setdefault), then
    ``tools.secret_store.api_key_from_env`` reads the (possibly DPAPI-protected)
    value. Returns "" when no usable key exists, so a caller can report an ``auth``
    hop failure instead of crashing.
    """
    if env_path:
        load_env_file(env_path)
    try:
        from tools.secret_store import api_key_from_env

        return api_key_from_env() or ""
    except Exception:
        return (os.environ.get("MCP_API_KEY") or "").strip()


def _post(url: str, payload: dict, key: str, sid: Optional[str] = None, timeout: float = 90.0):
    """POST one JSON-RPC message. Returns (status, session_id, elapsed_s, body).

    ``status`` is an int HTTP code, or the string ``"ERR"`` for a transport failure
    (timeout / connection refused). ``body`` is at most a few hundred characters and
    never contains the key (the key only ever rides in the Authorization header).
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if key:
        headers["Authorization"] = "Bearer " + key
    if sid:
        headers["Mcp-Session-Id"] = sid
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(400).decode("utf-8", "replace")
            return resp.status, resp.headers.get("Mcp-Session-Id"), time.time() - t0, body
    except urllib.error.HTTPError as exc:
        body = exc.read(300).decode("utf-8", "replace")
        return exc.code, None, time.time() - t0, body
    except Exception as exc:  # timeout / refused / DNS
        return "ERR", None, time.time() - t0, repr(exc)[:160]


def handshake(url: str, key: str, timeout: float = 90.0) -> dict:
    """Run the full MCP handshake against ``url`` and return a structured result.

    Keys in the returned dict:
      ok                  True only when initialize AND tools/list both returned 200.
      initialize_status   int HTTP code or "ERR".
      initialize_seconds  float.
      initialize_body     short body snippet (never the key).
      session             bool: did initialize hand back an Mcp-Session-Id.
      tools_list_status   int / "ERR" / None (None = not attempted because initialize failed).
      tools_list_seconds  float or None.
      tools_list_body     short snippet or None.
    """
    st, sid, dt, body = _post(
        url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "synthetic-monitor", "version": "1"},
            },
        },
        key,
        timeout=timeout,
    )
    result = {
        "ok": False,
        "initialize_status": st,
        "initialize_seconds": round(dt, 3),
        "initialize_body": body.replace("\n", " ")[:200],
        "session": bool(sid),
        "tools_list_status": None,
        "tools_list_seconds": None,
        "tools_list_body": None,
    }
    if st == 200:
        # The notification carries no id and expects no reply; failures here do not
        # change the verdict, which rests on initialize + tools/list.
        _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, key, sid, timeout=timeout)
        st2, _, dt2, body2 = _post(
            url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, key, sid, timeout=timeout
        )
        result["tools_list_status"] = st2
        result["tools_list_seconds"] = round(dt2, 3)
        result["tools_list_body"] = body2.replace("\n", " ")[:160]
        result["ok"] = st2 == 200
    return result


def _main(argv):
    import sys

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    key = resolve_api_key(os.path.join(repo_root, ".env"))
    targets = argv[1:] or [
        "http://127.0.0.1:8000/mcp",
        "https://x0wx6c3x-8000.jpe1.devtunnels.ms/mcp",
    ]
    for url in targets:
        print("==", url, time.strftime("%H:%M:%S"))
        res = handshake(url, key)
        print(
            "initialize ->", res["initialize_status"], "%.1fs" % res["initialize_seconds"],
            "session=" + str(res["session"]), res["initialize_body"],
        )
        if res["tools_list_status"] is not None:
            print(
                "tools/list ->", res["tools_list_status"],
                "%.1fs" % (res["tools_list_seconds"] or 0.0), res["tools_list_body"],
            )
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv))
