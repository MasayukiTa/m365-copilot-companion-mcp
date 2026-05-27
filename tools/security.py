import json
import os
import time
from pathlib import Path

from fastmcp.server.dependencies import get_http_request

STATE_FILE = Path(__file__).resolve().parent.parent / ".unlock_state.json"
TRUSTED_IPS = {"127.0.0.1", "::1", "localhost", ""}


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def get_client_ip() -> str:
    """Return the proxied client IP when available."""
    try:
        req = get_http_request()
    except Exception:
        return ""
    xff = req.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    client = getattr(req, "client", None)
    return client.host if client else ""


def is_trusted_local(ip: str) -> bool:
    return ip in TRUSTED_IPS


def is_unlocked(ip: str) -> bool:
    if is_trusted_local(ip):
        return True
    state = _load_state()
    entry = state.get(ip)
    if not entry:
        return False
    return entry.get("expires_at", 0) > time.time()


def require_unlocked() -> str | None:
    """Return None when the caller can use mutating tools, otherwise an error string."""
    ip = get_client_ip()
    if is_unlocked(ip):
        return None
    return (
        f"[locked client IP: {ip!r}] Mutating and execution tools require an unlock. "
        "Call unlock(password='<password>') first. The unlock is stored per client IP "
        "for MCP_UNLOCK_TTL_DAYS days."
    )


def unlock(password: str) -> str:
    """Unlock mutating and execution tools for the current remote client IP."""
    expected = os.environ.get("MCP_UNLOCK_PASSWORD", "")
    if not expected:
        return "[unlock error: MCP_UNLOCK_PASSWORD is not configured]"
    if password != expected:
        return "[unlock failed: incorrect password]"
    ip = get_client_ip()
    if not ip:
        return "[unlock failed: could not determine client IP]"
    if is_trusted_local(ip):
        return f"Local client {ip!r} is already trusted."
    ttl_days = int(os.environ.get("MCP_UNLOCK_TTL_DAYS", "30"))
    expires = time.time() + ttl_days * 86400
    state = _load_state()
    state[ip] = {"expires_at": expires, "unlocked_at": time.time()}
    _save_state(state)
    return f"Unlocked IP {ip!r} for {ttl_days} days."


def list_unlocked() -> str:
    """List remote IPs currently unlocked for mutating tools."""
    state = _load_state()
    if not state:
        return "(no unlocked remote IPs)"
    now = time.time()
    lines = []
    for ip, entry in state.items():
        exp = entry.get("expires_at", 0)
        remain_days = (exp - now) / 86400
        if remain_days <= 0:
            lines.append(f"{ip}: expired")
        else:
            lines.append(f"{ip}: {remain_days:.1f} days remaining")
    return "\n".join(lines)
