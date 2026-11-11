"""Permanent synthetic monitor for the MCP gateway.

WHY THIS EXISTS (measured 2026-11-11): the owner's chat client 'muse' reported the
MCP ``initialize`` handshake failing with HTTP 404 after about 60 s and no tool call
ever arriving, while our monitoring only did ``GET /health`` (200 everywhere) -- so
the monitoring watched something the client never does. The hypothesis, supported by
the supervisor log repeatedly showing 'tunnel host connections = 0' while the
devtunnel host process was alive, is: while the relay has no host connection it
answers 404 after waiting about 60 s. A /health probe cannot see that; only the REAL
handshake can.

This monitor runs the real handshake (initialize -> notifications/initialized ->
tools/list, see ``tools/mcp_handshake_probe.py``) against BOTH the local server and
the public devtunnel URL every 60 s. It treats a 404 OR a response slower than 30 s
as a failure, not only a non-200. It records status, latency and which hop failed,
correlates failures with the devtunnel host-connection count, probes the c2c
entrance, and raises a visible alert after 2 consecutive failures of either target.

The supervisor reads the JSON state this writes (``gateway_is_down``) to decide the
gateway is down FROM THIS MONITOR'S RESULT, not from /health.

The pure core (classification, correlation, alert and gateway-down decision) has no
I/O so CI can drive it with a fake server. The outer loop does the network, the
``devtunnel show`` call, the log reads and the JSONL writes.

No secret is ever logged: the API key only ever rides in the Authorization header of
the probe and is never written to the state file, the JSONL, or the alert.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tools.mcp_handshake_probe import handshake, resolve_api_key  # noqa: E402

# -- constants ------------------------------------------------------------------
LOCAL_URL = "http://127.0.0.1:8000/mcp"
TUNNEL_NAME = "resonac-mcp"
C2C_WELL_KNOWN = "https://codex-with-chatgpt.shuttle-scope.com/.well-known/oauth-protected-resource/mcp"
C2C_HEALTH = "https://codex-with-chatgpt.shuttle-scope.com/health"

#: A response is a failure when it is slower than this, even if it would be 200.
SLOW_THRESHOLD_S = 30.0
#: Consecutive failures of one target before a visible alert is raised.
FAILURES_BEFORE_ALERT = 2
#: Loop period.
INTERVAL_S = 60.0

_LOG_DIR = os.path.join(_REPO_ROOT, ".setup", "logs")
JSONL_PATH = os.path.join(_LOG_DIR, "mcp_handshake_monitor.jsonl")
STATE_PATH = os.path.join(_LOG_DIR, "mcp_handshake_monitor_state.json")
ALERT_PATH = os.path.join(_LOG_DIR, "mcp_handshake_monitor_alert.txt")
HOST_TS_LOG = os.path.join(_LOG_DIR, "devtunnel-host-ts.log")


# -- pure core ------------------------------------------------------------------
def is_failure(result: dict, slow_threshold_s: float = SLOW_THRESHOLD_S) -> bool:
    """True when a handshake result counts as a failure.

    A failure is any of: initialize not 200, tools/list not 200, a 404 anywhere, or
    EITHER leg slower than ``slow_threshold_s``. ``result`` is what
    ``mcp_handshake_probe.handshake`` returns.
    """
    if not result.get("ok"):
        return True
    init_s = result.get("initialize_seconds") or 0.0
    tl_s = result.get("tools_list_seconds") or 0.0
    if init_s > slow_threshold_s or tl_s > slow_threshold_s:
        return True
    if result.get("initialize_status") == 404 or result.get("tools_list_status") == 404:
        return True
    return False


def classify_hop(target: str, result: dict, host_connections: Optional[int],
                 slow_threshold_s: float = SLOW_THRESHOLD_S) -> str:
    """Name the hop that failed. One of:

      ok                 not a failure.
      local server       the loopback target itself failed (the server, not the relay).
      host connections=0  public target failed AND the relay reports 0 host connections
                         (the measured 404-after-60s cause -- the relay has no host).
      devtunnel relay    public target failed, host connections >= 1 (relay/path fault,
                         e.g. 404 or a >30s stall while a host is registered).
      auth               a 401/403 anywhere (bearer token rejected).
      slow               a 200 that was slower than the threshold.

    ``target`` is "local" or "public". ``host_connections`` is the devtunnel count or
    None when it could not be read.
    """
    if not is_failure(result, slow_threshold_s):
        return "ok"
    init_st = result.get("initialize_status")
    tl_st = result.get("tools_list_status")
    if init_st in (401, 403) or tl_st in (401, 403):
        return "auth"
    # A slow-but-200 exchange: the body is fine, the latency is not.
    if result.get("ok"):
        return "slow"
    if target == "local":
        return "local server"
    # public target failed.
    if host_connections is not None and host_connections < 1:
        return "host connections=0"
    return "devtunnel relay"


def parse_host_connections(show_output: str) -> Optional[int]:
    """The 'Host connections' integer from ``devtunnel show`` output, or None."""
    if not show_output:
        return None
    m = re.search(r"Host connections\s*:\s*(\d+)", show_output)
    if m:
        return int(m.group(1))
    return None


def update_streaks(prev: dict, local_fail: bool, public_fail: bool) -> dict:
    """Return the new {local_streak, public_streak} from the previous state."""
    return {
        "local_streak": (prev.get("local_streak", 0) + 1) if local_fail else 0,
        "public_streak": (prev.get("public_streak", 0) + 1) if public_fail else 0,
    }


def decide_alert(local_streak: int, public_streak: int,
                 failures_before_alert: int = FAILURES_BEFORE_ALERT) -> bool:
    """True when a visible alert should be raised (2 consecutive failures of a target)."""
    return (local_streak >= failures_before_alert) or (public_streak >= failures_before_alert)


def gateway_is_down(state: Optional[dict],
                    failures_before_alert: int = FAILURES_BEFORE_ALERT) -> bool:
    """The verdict the supervisor reads from the monitor's state file.

    The gateway is DOWN when either target has failed the real handshake
    ``failures_before_alert`` times in a row. A missing/unreadable state is NOT 'down'
    (absence of evidence must not restart-loop a healthy server); the caller falls
    back to its own liveness check in that case.
    """
    if not state:
        return False
    return decide_alert(
        int(state.get("local_streak", 0)),
        int(state.get("public_streak", 0)),
        failures_before_alert,
    )


def build_alert_text(state: dict) -> str:
    """One visible line stating what failed and whether a 404 coincided with host=0."""
    parts = []
    ls, ps = int(state.get("local_streak", 0)), int(state.get("public_streak", 0))
    if ls >= FAILURES_BEFORE_ALERT:
        parts.append("local server handshake failing (%d in a row, hop=%s)" % (ls, state.get("local_hop")))
    if ps >= FAILURES_BEFORE_ALERT:
        hc = state.get("host_connections")
        coincides = state.get("public_hop") == "host connections=0"
        status404 = state.get("public_initialize_status") == 404 or state.get("public_tools_list_status") == 404
        note = ""
        if status404 and coincides:
            note = " -- 404 COINCIDES with host connections = 0 (relay has no host)"
        elif status404:
            note = " -- 404 with host connections = %s" % hc
        parts.append("public URL handshake failing (%d in a row, hop=%s, host_connections=%s)%s"
                     % (ps, state.get("public_hop"), hc, note))
    if not parts:
        return ""
    return "MCP GATEWAY ALERT: " + "; ".join(parts)


# -- I/O ------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_host_connections(devtunnel_exe: str = "devtunnel",
                          tunnel_name: str = TUNNEL_NAME) -> Optional[int]:
    """Run ``devtunnel show`` and parse the host-connection count. None on any error."""
    try:
        out = subprocess.run(
            [devtunnel_exe, "show", tunnel_name],
            capture_output=True, text=True, timeout=25,
        ).stdout
    except Exception:
        return None
    return parse_host_connections(out)


def host_log_tail(path: str = HOST_TS_LOG, n: int = 3) -> str:
    """Last ``n`` non-empty lines of the devtunnel host log, for the correlation note."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return ""
    lines = [ln for ln in data.decode("utf-8", "replace").splitlines() if ln.strip()]
    return " | ".join(lines[-n:])


def probe_c2c(url: str, timeout: float = 20.0) -> dict:
    """Probe a c2c URL. curl needs --ssl-no-revoke on this PC; a bare 000 from curl
    here is a revocation-check false negative, so we try curl first and fall back to
    urllib. Returns {url, status, via}.
    """
    # curl with the revocation check disabled (the documented workaround on this PC).
    try:
        p = subprocess.run(
            ["curl", "-s", "-m", str(int(timeout)), "--ssl-no-revoke",
             "-o", os.devnull, "-w", "%{http_code}", url],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        code = (p.stdout or "").strip()
        if code and code != "000":
            return {"url": url, "status": int(code) if code.isdigit() else code, "via": "curl"}
    except Exception:
        pass
    # Fallback: urllib (a bare curl 000 is a revocation false negative here).
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"url": url, "status": resp.status, "via": "urllib"}
    except urllib.error.HTTPError as exc:
        return {"url": url, "status": exc.code, "via": "urllib"}
    except Exception as exc:
        return {"url": url, "status": "ERR", "via": "urllib", "error": repr(exc)[:120]}


def tunnel_url_from_env(env_path: Optional[str] = None) -> str:
    """MCP_TUNNEL_URL from .env, normalised so the handshake target ends in /mcp."""
    path = env_path or os.path.join(_REPO_ROOT, ".env")
    base = ""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("MCP_TUNNEL_URL") and "=" in line:
                    base = line.split("=", 1)[1].strip().strip('"')
                    break
    except OSError:
        return ""
    if not base:
        return ""
    base = base.rstrip("/")
    return base + "/mcp"


def _atomic_write_json(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    os.replace(tmp, path)


def run_once(prev_state: dict, env_path: Optional[str] = None,
             local_url: str = LOCAL_URL) -> dict:
    """One monitor tick. Returns the new state dict (also written to STATE_PATH).

    Pure-ish: all the decision logic is the pure core above; this function only
    gathers the network/CLI/log facts and threads them through it.
    """
    os.makedirs(_LOG_DIR, exist_ok=True)
    key = resolve_api_key(env_path or os.path.join(_REPO_ROOT, ".env"))
    public_url = tunnel_url_from_env(env_path)
    host_conn = read_host_connections()

    local_res = handshake(local_url, key, timeout=SLOW_THRESHOLD_S + 60.0)
    public_res = (handshake(public_url, key, timeout=SLOW_THRESHOLD_S + 60.0)
                  if public_url else {"ok": False, "initialize_status": "nourl",
                                      "initialize_seconds": 0.0, "initialize_body": "MCP_TUNNEL_URL not set",
                                      "session": False, "tools_list_status": None,
                                      "tools_list_seconds": None, "tools_list_body": None})

    local_fail = is_failure(local_res)
    public_fail = is_failure(public_res)
    local_hop = classify_hop("local", local_res, host_conn)
    public_hop = classify_hop("public", public_res, host_conn)

    streaks = update_streaks(prev_state, local_fail, public_fail)
    c2c = [probe_c2c(C2C_WELL_KNOWN), probe_c2c(C2C_HEALTH)]

    state = {
        "ts": _now_iso(),
        "local_fail": local_fail,
        "public_fail": public_fail,
        "local_hop": local_hop,
        "public_hop": public_hop,
        "local_initialize_status": local_res.get("initialize_status"),
        "local_tools_list_status": local_res.get("tools_list_status"),
        "local_initialize_seconds": local_res.get("initialize_seconds"),
        "local_tools_list_seconds": local_res.get("tools_list_seconds"),
        "public_initialize_status": public_res.get("initialize_status"),
        "public_tools_list_status": public_res.get("tools_list_status"),
        "public_initialize_seconds": public_res.get("initialize_seconds"),
        "public_tools_list_seconds": public_res.get("tools_list_seconds"),
        "host_connections": host_conn,
        "host_log_tail": host_log_tail(),
        "c2c": c2c,
        "local_streak": streaks["local_streak"],
        "public_streak": streaks["public_streak"],
    }
    state["gateway_is_down"] = gateway_is_down(state)
    state["alert"] = build_alert_text(state)

    # append one JSONL line, write the latest-state file, and the visible alert file.
    try:
        with open(JSONL_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(state, ensure_ascii=False) + "\n")
    except OSError:
        pass
    _atomic_write_json(STATE_PATH, state)
    if state["alert"]:
        try:
            with open(ALERT_PATH, "w", encoding="utf-8") as fh:
                fh.write(state["ts"] + " " + state["alert"] + "\n")
        except OSError:
            pass
    return state


def main(argv=None) -> int:
    argv = argv or sys.argv
    once = "--once" in argv
    prev = {}
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as fh:
                prev = json.load(fh)
        except Exception:
            prev = {}
    while True:
        state = run_once(prev)
        prev = state
        line = "%s local=%s(%s) public=%s(%s) host_conn=%s%s" % (
            state["ts"],
            "FAIL" if state["local_fail"] else "ok", state["local_hop"],
            "FAIL" if state["public_fail"] else "ok", state["public_hop"],
            state["host_connections"],
            ("  " + state["alert"]) if state["alert"] else "",
        )
        print(line, flush=True)
        if once:
            return 0
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    raise SystemExit(main())
