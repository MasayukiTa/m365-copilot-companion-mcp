"""Send the REAL MCP handshake to the local server and the public tunnel, and classify the hop.

This is the permanent home of the reference probe (scratchpad/mcp_probe.py, hs_loop.py). Every
60 s a supervisor-side caller runs ``probe_once`` for BOTH
``http://127.0.0.1:8000/mcp`` and ``MCP_TUNNEL_URL`` + ``/mcp``:

    initialize -> notifications/initialized -> tools/list

A 404, any non-200, or a response slower than 30 s is a failure, and the failing hop is named
by ``scripts.tunnel_login_state`` (local server down / public relay no host / auth / slow).

IT NEVER CALLS THE devtunnel CLI. Login state is decided from the token FILE (size + mtime)
via ``read_token_login_state`` -> ``decide_login_state``; host state is decided from the
public probe result and the supervisor log, not from ``devtunnel user show`` / ``devtunnel
show``. Two logins were lost within a minute of such CLI bursts (measured 2026-11-11), and the
one login that survived 4+ hours did so while NOTHING called the devtunnel CLI. The test
``scripts/test_probe_never_shells_out_to_devtunnel.py`` fails if this module or
tunnel_login_state.py ever shells out to devtunnel.

The bearer token is resolved exactly as the server does -- load .env, then
``tools.secret_store.require_api_key`` -- and is NEVER printed.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from tunnel_login_state import (  # noqa: E402  (scripts/ is on sys.path for the caller)
    classify_local_hop,
    classify_public_hop,
    decide_login_state,
)

#: The devtunnel token cache. Its SIZE decides login state (4182 bytes valid / 0 bytes out,
#: measured 2026-11-11). Read with os.stat only -- never the CLI.
_TOKEN_REL = ("DevTunnels", "devtunnels-tokens-microsoft")


def token_file_path(localappdata=None):
    """Absolute path to the devtunnel token cache, from %LOCALAPPDATA% (no subprocess)."""
    base = localappdata if localappdata is not None else os.environ.get("LOCALAPPDATA", "")
    return os.path.join(base, *_TOKEN_REL)


def read_token_login_state(path=None, now=None):
    """Login state from the token file on disk, using os.stat only -- never the devtunnel CLI.

    Returns one of the ``LOGIN_*`` strings from tunnel_login_state. A missing file or a stat
    error is reported as logged-out / unknown by ``decide_login_state``; this function does
    not raise and does not shell out.
    """
    p = path if path is not None else token_file_path()
    try:
        st = os.stat(p)
        size, mtime = st.st_size, st.st_mtime
    except FileNotFoundError:
        size, mtime = None, None
    except OSError:
        size, mtime = None, None
    return decide_login_state(size, mtime, now=time.time() if now is None else now)


def _post(url, payload, key, sid=None, timeout=90):
    """POST one JSON-RPC message. Returns (status, session_id, elapsed_s, short_body).

    status is an int HTTP code, or the string "ERR" on timeout / refused. The bearer token is
    only ever placed in the Authorization header; it is never returned or logged.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": "Bearer " + key,
    }
    if sid:
        headers["Mcp-Session-Id"] = sid
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(400).decode("utf-8", "replace")
            return r.status, r.headers.get("Mcp-Session-Id"), time.time() - t0, body
    except urllib.error.HTTPError as e:
        body = e.read(300).decode("utf-8", "replace")
        return e.code, None, time.time() - t0, body
    except Exception as e:  # timeout / refused
        return "ERR", None, time.time() - t0, repr(e)[:160]


def probe_once(url, key, is_public, login_state, timeout=90):
    """Run the real handshake against ONE url and classify the hop.

    Returns a dict: {url, status, elapsed_s, hop, cause, ok}. ``ok`` is True only for a fast
    200 on initialize AND tools/list. The hop/cause come from the login-state-aware
    classifiers in tunnel_login_state, so an alert can name the hop and the likely cause.
    """
    st, sid, dt, _body = _post(url, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "gateway-probe", "version": "1"}}}, key,
        timeout=timeout)
    classify = classify_public_hop if is_public else classify_local_hop
    if is_public:
        hop, cause = classify(st, dt, login_state)
    else:
        hop, cause = classify(st, dt)
    result = {"url": url, "status": st, "elapsed_s": round(dt, 2),
              "hop": hop, "cause": cause, "ok": False}
    if st == 200:
        # Complete the handshake so tools/list is genuinely exercised, matching a real client.
        _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, key, sid,
              timeout=timeout)
        st2, _s2, dt2, _b2 = _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                                   key, sid, timeout=timeout)
        if is_public:
            hop2, cause2 = classify(st2, dt2, login_state)
        else:
            hop2, cause2 = classify(st2, dt2)
        if hop2 != "ok":
            result.update(status=st2, elapsed_s=round(dt2, 2), hop=hop2, cause=cause2)
        else:
            result["ok"] = True
    return result


def _default_urls():
    """The two endpoints the supervisor probes: local main.py and the public tunnel.

    The public URL comes from MCP_TUNNEL_URL (the same variable the gateway publishes under);
    when it is unset only the local hop is probed. No subprocess, no devtunnel CLI.
    """
    urls = [("http://127.0.0.1:8000/mcp", False)]
    pub = os.environ.get("MCP_TUNNEL_URL", "").strip().rstrip("/")
    if pub:
        urls.append((pub + "/mcp", True))
    return urls


def _resolve_key():
    """The bearer token, resolved exactly as the server does, and never printed.

    Loads .env if python-dotenv is present, then tools.secret_store.require_api_key. Returns
    an empty string when no key is configured so main() can report that without raising.
    """
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()
    except Exception:
        pass
    try:
        from tools.secret_store import require_api_key  # type: ignore
        return require_api_key() or ""
    except Exception:
        return os.environ.get("MCP_API_KEY", "") or ""


def main(argv=None):
    """Probe BOTH hops once and print one JSON line per hop; return non-zero if any failed.

    This is the supervisor-side entry: it reads login state from the token FILE (via
    read_token_login_state -> token_file_path + decide_login_state), then runs the real
    handshake against each url with probe_once. It NEVER calls the devtunnel CLI and never
    prints the bearer token.
    """
    login_state = read_token_login_state()
    key = _resolve_key()
    failed = False
    for url, is_public in _default_urls():
        res = probe_once(url, key, is_public, login_state)
        res["login_state"] = login_state
        print(json.dumps(res, ensure_ascii=False))
        if not res.get("ok"):
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
