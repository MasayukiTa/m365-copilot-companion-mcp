import json
import os
import time
from pathlib import Path

from fastmcp.server.dependencies import get_http_request

from tools.secret_store import unlock_password_from_env

STATE_FILE = Path(__file__).resolve().parent.parent / ".unlock_state.json"

# IPs that are always trusted when they appear as the *real connection peer*
# (req.client.host) with NO X-Forwarded-For header present.
# "" is intentionally excluded: an unknown/empty peer must fail CLOSED.
TRUSTED_LOCAL_PEERS = {"127.0.0.1", "::1", "localhost"}


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _parse_request(req) -> tuple[bool, str]:
    """Return (is_genuine_local, identity_ip).

    is_genuine_local = True only when:
      - the TCP peer (req.client.host) is a loopback address, AND
      - there is NO X-Forwarded-For header at all.
    A request that carries any XFF came through a proxy/tunnel and MUST NOT be
    treated as trusted-local, even if the XFF value happens to say 127.0.0.1.

    identity_ip is the IP used for per-IP unlock lookup when the caller is not
    a genuine local:
      - When XFF is present we use the entry MCP_TRUSTED_PROXY_HOPS hops from
        the RIGHT (default 1). This is the address the last trusted proxy
        observed, not the leftmost client-supplied entry.

        Rationale for default=1: inspection of .unlock_state.json shows stored
        IPs are real external addresses like 20.210.x / 210.157.x — single-entry
        XFF values coming from the Dev Tunnel (the tunnel does NOT append its own
        hop). With hops=1, index[-1] == index[0], so existing stored unlocks
        continue to match. If a second trusted proxy is added in future, raise
        MCP_TRUSTED_PROXY_HOPS to 2.
      - When there is no XFF, identity_ip is req.client.host (direct connection).
    """
    client = getattr(req, "client", None)
    peer = client.host if client else ""

    xff = req.headers.get("x-forwarded-for", "")
    if not xff:
        # No forwarding header: genuine local if peer is loopback.
        is_local = peer in TRUSTED_LOCAL_PEERS
        return is_local, peer

    # XFF present — always a proxied/tunnel request; never treat as local.
    hops = int(os.environ.get("MCP_TRUSTED_PROXY_HOPS", "1"))
    entries = [e.strip() for e in xff.split(",") if e.strip()]
    if not entries:
        return False, ""
    # Take the entry `hops` from the right; clamp to index 0 if fewer entries.
    idx = max(0, len(entries) - hops)
    return False, entries[idx]


def get_client_ip() -> str:
    """Return the identity IP for the current request (used for unlock lookup).

    Kept for backward-compat with callers that just need the IP string.
    For security decisions (is_genuine_local) use _parse_request() directly.
    """
    try:
        req = get_http_request()
    except Exception:
        return ""
    _, ip = _parse_request(req)
    return ip


def is_trusted_local(ip: str) -> bool:
    """Legacy helper — do NOT use for security decisions; use _parse_request()."""
    return ip in TRUSTED_LOCAL_PEERS


def is_unlocked(ip: str) -> bool:
    """Check whether `ip` is allowed to use mutating tools.

    NOTE: this does not check is_genuine_local; callers should use
    require_unlocked() which handles the full request context.
    """
    state = _load_state()
    entry = state.get(ip)
    if not entry:
        return False
    return entry.get("expires_at", 0) > time.time()


def require_unlocked() -> str | None:
    """Return None when the caller can use mutating tools, otherwise an error string."""
    try:
        req = get_http_request()
    except Exception:
        # No HTTP request context (e.g. called from a test or CLI): deny.
        return (
            "[locked: no HTTP request context] "
            "Call unlock(password='<password>') first."
        )
    is_local, ip = _parse_request(req)
    if is_local:
        return None
    if is_unlocked(ip):
        return None
    return (
        f"[locked client IP: {ip!r}] Mutating and execution tools require an unlock. "
        "Call unlock(password='<password>') first. The unlock is stored per client IP "
        "for MCP_UNLOCK_TTL_DAYS days."
    )


def unlock(password: str) -> str:
    """Unlock mutating and execution tools for the current remote client IP."""
    expected = unlock_password_from_env()
    if not expected:
        return "[unlock error: MCP_UNLOCK_PASSWORD is not configured]"
    if password != expected:
        return "[unlock failed: incorrect password]"
    try:
        req = get_http_request()
    except Exception:
        return "[unlock failed: no HTTP request context]"
    is_local, ip = _parse_request(req)
    if not ip:
        return "[unlock failed: could not determine client IP]"
    if is_local:
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
