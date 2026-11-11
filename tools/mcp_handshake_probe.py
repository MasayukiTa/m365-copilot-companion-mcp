# -*- coding: utf-8 -*-
r"""Resident synthetic MCP-handshake monitor (the I/O side).

This is the part that actually TALKS to the network: it sends the real MCP handshake
(``initialize`` -> ``notifications/initialized`` -> ``tools/list``) against BOTH the local
server (http://127.0.0.1:8000/mcp) and the PUBLIC URL (MCP_TUNNEL_URL from .env, plus
/mcp), measures status and latency, reads the devtunnel LOGIN state from the token FILE
(size + mtime -- never the devtunnel CLI), probes the c2c entrance, and hands every measured
fact to the PURE decision core in
scripts/mcp_handshake_monitor.py, which decides the words (pass/fail, which hop, alert,
404-vs-zero-host correlation, gateway-down verdict).

It NEVER prints the API key. The key is resolved exactly the way the server resolves it
(tools/secret_store.require_api_key after loading .env), and only ever placed in an
Authorization header -- never logged, never echoed, never written to the status file.

The handshake wire format is copied verbatim from the reference probe that was measured to
work on 2026-11-11 (initialize 200 in 1.2 s locally and through the devtunnel URL).

Outputs (under .setup/logs/, beside the server's own telemetry):
  * mcp_synthetic_monitor.jsonl  -- one JSON line per tick (status, latency, hop, alert...)
  * mcp_synthetic_monitor_status.json -- the latest tick only, for the cockpit / supervisor
  * mcp_synthetic_monitor_alert.json  -- present ONLY while an alert is active (>=2 fails)

This file does NOT run on the CI runner: it is Windows-resident and does real network I/O.
The hermetic tests exercise scripts/mcp_handshake_monitor.py (pure) instead.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts import mcp_handshake_monitor as core  # noqa: E402
from scripts import tunnel_login_state as login_core  # noqa: E402  (pure token-file login decider; never the CLI)
from tools import childproc  # noqa: E402  (headless_creationflags: these children must never pop a console)

LOCAL_URL = "http://127.0.0.1:8000/mcp"
#: The c2c entrance base URL is read from the environment (never hard-coded here, so no
#: host identifier lives in the tree). When unset, the c2c probe is reported as not
#: configured and skipped. Set C2C_ENTRANCE_URL in .env to enable it.
C2C_ENTRANCE = os.environ.get("C2C_ENTRANCE_URL", "").rstrip("/")

#: How long a single initialize POST may run before we stop waiting. Above the ~60 s the
#: relay was measured to wait before a 404 so that a 404-after-60 s is observed as a 404,
#: not hidden as a client-side timeout; the slowness verdict (> SLOW_SECONDS) is what turns
#: a genuinely slow success into a failure.
REQUEST_TIMEOUT = 75


def _load_dotenv(root):
    """Load .env into the environment the way the server does. Values are never printed."""
    envp = os.path.join(root, ".env")
    if not os.path.exists(envp):
        return
    for line in open(envp, encoding="utf-8-sig"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


def _resolve_key():
    """Resolve the API key exactly as the server does. Returns the key or None; never logs it."""
    try:
        from tools.secret_store import require_api_key
        return require_api_key()
    except Exception:
        # Fall back to the raw env var name the server uses, still without printing it.
        return os.environ.get("MCP_API_KEY") or None


def _public_url():
    """MCP_TUNNEL_URL + /mcp, or None when not configured."""
    base = (os.environ.get("MCP_TUNNEL_URL") or "").strip().rstrip("/")
    if not base:
        return None
    if base.endswith("/mcp"):
        return base
    return base + "/mcp"


def _post(url, payload, key, sid=None, timeout=REQUEST_TIMEOUT):
    """POST one JSON-RPC message. Returns (status, session_id, latency_s, short_body).

    status is an int on an HTTP answer, or the string "ERR" on timeout/refused. The API key
    goes only into the Authorization header and is never returned or logged.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if key:
        headers["Authorization"] = "Bearer " + key
    if sid:
        headers["Mcp-Session-Id"] = sid
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(400).decode("utf-8", "replace")
            return r.status, r.headers.get("Mcp-Session-Id"), time.time() - t0, body
    except urllib.error.HTTPError as e:
        body = e.read(300).decode("utf-8", "replace")
        return e.code, None, time.time() - t0, body
    except Exception as e:  # timeout / refused / dns
        return "ERR", None, time.time() - t0, repr(e)[:160]


def handshake(url, key, timeout=REQUEST_TIMEOUT):
    """Run the full handshake against one URL. Returns a dict of measured facts (no key).

    {initialize_status, initialize_latency_s, session_established(bool),
     tools_list_status, tools_list_latency_s}
    """
    st, sid, dt, _body = _post(url, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "synthetic-monitor", "version": "1"}}}, key, timeout=timeout)
    out = {
        "initialize_status": st,
        "initialize_latency_s": round(dt, 3),
        "session_established": bool(sid),
        "tools_list_status": None,
        "tools_list_latency_s": None,
    }
    if st == 200:
        _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, key, sid, timeout=timeout)
        st2, _, dt2, _b2 = _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, key, sid, timeout=timeout)
        out["tools_list_status"] = st2
        out["tools_list_latency_s"] = round(dt2, 3)
    return out


def _token_file_path():
    """Path to the devtunnel token FILE, or None. A path string only -- never the CLI.

    %LOCALAPPDATA%\\DevTunnels\\devtunnels-tokens-microsoft (measured 2026-11-11: 4182 bytes
    when a login is valid, 0 bytes when logged out). Reading this file's os.stat is the ONLY
    way this module learns login state; it must never run a devtunnel subprocess, because
    concurrent devtunnel CLI bursts were observed to drop a fresh login within a minute.
    """
    base = os.environ.get("LOCALAPPDATA") or ""
    if not base:
        return None
    return os.path.join(base, "DevTunnels", "devtunnels-tokens-microsoft")


def read_login_state():
    """Devtunnel LOGIN state decided from the token FILE alone -- never the devtunnel CLI.

    Reads the token file's size and mtime with os.stat (no subprocess) and hands them to the
    pure decider scripts/tunnel_login_state.decide_login_state. Returns one of
    login_core.LOGIN_VALID / LOGIN_NONE / LOGIN_UNKNOWN. Whether the relay actually has a live
    host is answered by the PUBLIC handshake result (404 vs 200), not by this file.
    """
    path = _token_file_path()
    if not path:
        return login_core.LOGIN_UNKNOWN
    try:
        stt = os.stat(path)
        size, mtime = stt.st_size, stt.st_mtime
    except FileNotFoundError:
        size, mtime = None, None
    except OSError:
        return login_core.LOGIN_UNKNOWN
    return login_core.decide_login_state(size, mtime, now=time.time())


def probe_c2c_entrance(timeout=20):
    """Probe the c2c entrance /.well-known/... and /health. curl needs --ssl-no-revoke here;
    a bare 000 from curl on this PC is a revocation-check false negative, reported as such.
    """
    if not C2C_ENTRANCE:
        return {"configured": False}
    results = {"configured": True}
    for path in ("/.well-known/oauth-protected-resource/mcp", "/health"):
        url = C2C_ENTRANCE + path
        try:
            # headless_creationflags: unattended curl, reads output only (never prompts), so it
            # must not allocate a console on a windowless desktop. encoding/errors stated so the
            # decode is not left to the locale (the child-output ratchet).
            cp = subprocess.run(
                ["curl", "--ssl-no-revoke", "-s", "-o", os.devnull, "-w", "%{http_code}",
                 "--max-time", str(timeout), url],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout + 5,
                creationflags=childproc.headless_creationflags())
            code = (cp.stdout or "").strip()
        except Exception as e:
            code = "ERR:" + repr(e)[:60]
        note = ""
        if code == "000":
            note = "curl 000 = revocation-check false negative on this PC, not a real failure"
        results[path] = {"http_code": code, "note": note}
    return results


def run_tick(key, tunnel_name, prev_counts):
    """One monitoring tick. Returns (record, new_counts). prev_counts is {scope: int}."""
    local_raw = handshake(LOCAL_URL, key)
    local_result = core.classify_handshake(local_raw["initialize_status"], local_raw["initialize_latency_s"])
    local_hop = core.classify_hop("local", local_result)

    pub_url = _public_url()
    if pub_url:
        public_raw = handshake(pub_url, key)
        # Decide host state from the PUBLIC handshake result, and login state from the token
        # FILE -- never from a devtunnel subprocess. host_connections is left None because the
        # exact count is only obtainable via the CLI we are forbidden to call; a 404 with a
        # logged-out token is the 'relay has no host' signal the alert reports instead.
        host_conn = None
        login_state = read_login_state()
        public_result = core.classify_handshake(public_raw["initialize_status"], public_raw["initialize_latency_s"])
        public_hop = core.classify_hop("public", public_result, host_connections=host_conn)
    else:
        public_raw, public_result, public_hop, host_conn = None, None, core.HOP_UNKNOWN, None
        login_state = read_login_state()

    new_counts = {
        "local": core.update_consecutive_failures(prev_counts.get("local"), local_result),
        "public": core.update_consecutive_failures(prev_counts.get("public"), public_result),
    }
    local_alert = core.decide_alert(new_counts["local"])
    public_alert = core.decide_alert(new_counts["public"])
    gateway = core.gateway_is_down(local_result, public_result)
    four_oh_four_zero_host = core.correlates_404_with_zero_host_connections(public_result, host_conn)

    record = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "local": {"raw": local_raw, "result": local_result, "hop": local_hop,
                  "consecutive_failures": new_counts["local"], "alert": local_alert},
        "public": {"url_configured": bool(pub_url), "raw": public_raw, "result": public_result,
                   "hop": public_hop, "host_connections": host_conn,
                   "login_state": login_state,
                   "consecutive_failures": new_counts["public"], "alert": public_alert},
        "gateway": gateway,
        "four_oh_four_coincides_with_zero_host_connections": four_oh_four_zero_host,
        "c2c": probe_c2c_entrance(),
    }
    return record, new_counts


def _alert_sentence(record):
    """One human sentence for the alert file, stating whether 404 met host connections = 0."""
    pub = record["public"]
    parts = []
    if pub.get("alert"):
        parts.append("PUBLIC MCP handshake failed %d consecutive times (hop=%s)"
                     % (pub["consecutive_failures"], pub["hop"]))
    if record["local"].get("alert"):
        parts.append("LOCAL MCP handshake failed %d consecutive times"
                     % record["local"]["consecutive_failures"])
    if record["four_oh_four_coincides_with_zero_host_connections"]:
        parts.append("the 404 COINCIDES with devtunnel host connections = 0 "
                     "(relay has no host connection)")
    elif pub.get("host_connections") is not None:
        parts.append("devtunnel host connections = %d" % pub["host_connections"])
    # Login state is read from the token FILE (never the CLI); a logged-out token beside a
    # public failure is the 'relay has no host' cause the operator must see.
    if pub.get("login_state") == login_core.LOGIN_NONE:
        parts.append("devtunnel token FILE is empty/absent = logged out (relay will have no host)")
    return "; ".join(parts) or "MCP handshake monitor alert"


def _logs_dir(root):
    d = os.path.join(root, ".setup", "logs")
    os.makedirs(d, exist_ok=True)
    return d


def write_outputs(root, record):
    """Append the tick to the jsonl log, overwrite the status file, and raise/clear the alert."""
    logs = _logs_dir(root)
    with open(os.path.join(logs, "mcp_synthetic_monitor.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    with open(os.path.join(logs, "mcp_synthetic_monitor_status.json"), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    alert_path = os.path.join(logs, "mcp_synthetic_monitor_alert.json")
    if record["local"].get("alert") or record["public"].get("alert"):
        with open(alert_path, "w", encoding="utf-8") as f:
            json.dump({"ts": record["ts"], "message": _alert_sentence(record),
                       "gateway": record["gateway"]}, f, ensure_ascii=False, indent=2)
    elif os.path.exists(alert_path):
        try:
            os.remove(alert_path)
        except OSError:
            pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    interval = 60
    once = False
    for a in argv:
        if a == "--once":
            once = True
        elif a.startswith("--interval="):
            try:
                interval = int(a.split("=", 1)[1])
            except ValueError:
                pass
    _load_dotenv(_ROOT)
    key = _resolve_key()
    tunnel_name = (os.environ.get("MCP_TUNNEL_NAME") or "").strip() or None
    counts = {"local": 0, "public": 0}
    while True:
        record, counts = run_tick(key, tunnel_name, counts)
        write_outputs(_ROOT, record)
        # Never print the key; a compact one-line status is safe (statuses and hops only).
        print("[mcp-monitor] %s local=%s public=%s gateway_down=%s 404@0host=%s" % (
            record["ts"],
            record["local"]["result"],
            (record["public"]["result"] if record["public"]["result"] else "n/a"),
            record["gateway"]["gateway_down"],
            record["four_oh_four_coincides_with_zero_host_connections"]))
        if once:
            return 0
        time.sleep(max(5, interval))


if __name__ == "__main__":
    raise SystemExit(main())
