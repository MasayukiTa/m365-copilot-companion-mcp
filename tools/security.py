import ipaddress
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from fastmcp.server.dependencies import get_http_request

from tools.secret_store import unlock_password_from_env

# Records lock refusals so readers never have to infer them from agent prose.
from tools import lock_state

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


def derive_identity(peer_host: str, xff_header_value: str) -> tuple[bool, str]:
    """Pure IP-derivation logic: the ONE place that decides how a caller's
    identity IP is computed from a raw TCP peer address plus an
    X-Forwarded-For header value. Both plain strings, no framework types --
    this is what makes it callable from _parse_request() (which has a
    Starlette/FastMCP Request) AND from main.py's outermost ASGI middleware
    (which only has a raw `scope` dict, no Request object). If those two call
    sites ever computed the identity IP differently, the auth-failure sidecar
    would record an IP that does not match the one the unlock gate actually
    checks against -- making the recorded data useless. Do not reimplement
    this logic anywhere else; add a new call site instead.

    Returns (is_genuine_local, identity_ip).

    is_genuine_local = True only when:
      - peer_host is a loopback address, AND
      - xff_header_value is empty (no X-Forwarded-For header at all).
    A request that carries any XFF came through a proxy/tunnel and MUST NOT be
    treated as trusted-local, even if the XFF value happens to say 127.0.0.1.

    identity_ip is the IP used for per-IP unlock lookup (and, separately, for
    the auth-failure sidecar breakdown) when the caller is not a genuine local:
      - When XFF is present we use the entry MCP_TRUSTED_PROXY_HOPS hops from
        the RIGHT (default 1). This is the address the last trusted proxy
        observed, not the leftmost client-supplied entry.

        Rationale for default=1: inspection of .unlock_state.json shows stored
        IPs are real external addresses like 20.210.x / 210.157.x — single-entry
        XFF values coming from the Dev Tunnel (the tunnel does NOT append its own
        hop). With hops=1, index[-1] == index[0], so existing stored unlocks
        continue to match. If a second trusted proxy is added in future, raise
        MCP_TRUSTED_PROXY_HOPS to 2.
      - When there is no XFF, identity_ip is peer_host (direct connection).
    """
    peer = peer_host or ""
    xff = xff_header_value or ""

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


def _parse_request(req) -> tuple[bool, str]:
    """Return (is_genuine_local, identity_ip) for a Starlette/FastMCP Request.

    Thin adapter over derive_identity(): pulls the peer host and the raw
    X-Forwarded-For header value off `req` and hands them to the shared, pure
    derivation function. See derive_identity()'s docstring for the full
    rationale (loopback-only trust, MCP_TRUSTED_PROXY_HOPS, etc). Do not
    duplicate that logic here -- add call sites against derive_identity()
    instead, so there remains exactly one place that decides how the IP is
    derived.
    """
    client = getattr(req, "client", None)
    peer = client.host if client else ""
    xff = req.headers.get("x-forwarded-for", "")
    return derive_identity(peer, xff)


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
        msg = (
            "[locked: no HTTP request context] "
            "Call unlock(password='<password>') first."
        )
        lock_state.record_locked("", msg)
        return msg
    is_local, ip = _parse_request(req)
    if is_local:
        return None
    if is_unlocked(ip):
        return None
    msg = (
        f"[locked client IP: {ip!r}] Mutating and execution tools require an unlock. "
        "Call unlock(password='<password>') first. The unlock is stored per client IP "
        "for MCP_UNLOCK_TTL_DAYS days."
    )
    lock_state.record_locked(ip, msg)
    return msg


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
    # The refusal that prompted this unlock is now history; drop it so a reader
    # checking "was a call just refused?" is not answered by a stale record.
    lock_state.clear()
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


# ─────────────────────────────────────────────────────────────────────────────────────
# Local (non-HTTP) per-IP unlock admin -- for the cockpit UI ONLY.
#
# unlock() above only ever authorises the CALLER's own IP, derived from a live HTTP
# request (_parse_request(get_http_request())). That is right for the MCP tool -- a
# remote agent can only unlock itself, never grant access to some other address -- but
# it means unlock() cannot be reused to let a human sitting at this machine authorise a
# *different* client from the desktop cockpit after watching it get refused. The
# functions below fill that gap. They:
#
#   * take the target IP as a plain argument instead of deriving it from a request;
#   * are local Python calls (imported from a script / invoked via `python -m
#     tools.security ...`), never registered as MCP tools -- so a remote model can never
#     call them and grant itself access;
#   * do not touch require_unlocked(), unlock(), or _parse_request() -- the unlock gate
#     that mutating tools go through is completely unchanged;
#   * write .unlock_state.json atomically (tmp file + os.replace), because the running
#     MCP server process reads and writes the very same file on every unlock() call and
#     a half-written file must never be observable.
# ─────────────────────────────────────────────────────────────────────────────────────

_GRANT_LOCK = threading.Lock()


def _valid_ip(ip: str) -> bool:
    """True iff `ip` parses as a real IPv4 or IPv6 address. Rejects "", whitespace, and
    garbage rather than letting it become a junk key in .unlock_state.json."""
    ip = (ip or "").strip()
    if not ip:
        return False
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def _save_state_atomic(state: dict) -> None:
    """Same on-disk shape and location as _save_state(), but tmp-file + os.replace so a
    concurrent unlock() write from the live server process can never observe (or produce)
    a partially-written file. _save_state() itself is left untouched -- unlock()'s own
    write path is out of scope for this change."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(STATE_FILE.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def grant_ip(ip: str, ttl_days: float | None = None) -> dict:
    """LOCAL operator action: create or extend an unlock for `ip`, exactly like unlock()
    would for the caller's own address, but callable from the desktop cockpit for an
    arbitrary remote client. Same TTL rule as unlock(): MCP_UNLOCK_TTL_DAYS (default 30)
    unless overridden. Raises ValueError for an empty/garbage IP rather than writing junk.

    Returns {"ip", "expires_at", "unlocked_at", "ttl_days"}.
    """
    ip = (ip or "").strip()
    if not _valid_ip(ip):
        raise ValueError(f"invalid IP address: {ip!r}")
    if ttl_days is None:
        ttl_days = float(os.environ.get("MCP_UNLOCK_TTL_DAYS", "30"))
    ttl_days = float(ttl_days)
    with _GRANT_LOCK:
        state = _load_state()
        now = time.time()
        expires = now + ttl_days * 86400
        state[ip] = {"expires_at": expires, "unlocked_at": now}
        _save_state_atomic(state)
    return {"ip": ip, "expires_at": expires, "unlocked_at": now, "ttl_days": ttl_days}


def revoke_ip(ip: str) -> bool:
    """LOCAL operator action: remove any unlock for `ip`. Returns True if an entry was
    removed, False if `ip` had no entry (a no-op, not an error). Raises ValueError for an
    empty/garbage IP."""
    ip = (ip or "").strip()
    if not _valid_ip(ip):
        raise ValueError(f"invalid IP address: {ip!r}")
    with _GRANT_LOCK:
        state = _load_state()
        existed = ip in state
        if existed:
            del state[ip]
            _save_state_atomic(state)
    return existed


def list_grants() -> list[dict]:
    """Every entry in .unlock_state.json as a plain list, newest-expiring first, each with
    a pre-computed remaining_seconds/expired so callers (the cockpit UI, tests) never have
    to redo the clock math list_unlocked()'s string form hides."""
    state = _load_state()
    now = time.time()
    out = []
    for ip, entry in state.items():
        expires_at = float(entry.get("expires_at", 0) or 0)
        remaining = expires_at - now
        out.append({
            "ip": ip,
            "expires_at": expires_at,
            "unlocked_at": entry.get("unlocked_at"),
            "remaining_seconds": remaining,
            "expired": remaining <= 0,
        })
    out.sort(key=lambda r: -r["expires_at"])
    return out


def _cli() -> None:
    """`python -m tools.security <list|grant|revoke>` -- the local admin surface the
    cockpit's 詳細設定/Advanced panel shells out to. Never registered as an MCP tool."""
    import argparse

    ap = argparse.ArgumentParser(
        description="Local (non-HTTP) per-IP unlock admin. Not an MCP tool.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    g = sub.add_parser("grant")
    g.add_argument("ip")
    g.add_argument("--ttl-days", type=float, default=None)
    r = sub.add_parser("revoke")
    r.add_argument("ip")

    args = ap.parse_args()
    if args.cmd == "list":
        print(json.dumps(list_grants(), ensure_ascii=False))
        return
    if args.cmd == "grant":
        try:
            print(json.dumps(grant_ip(args.ip, args.ttl_days), ensure_ascii=False))
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
            raise SystemExit(1)
        return
    if args.cmd == "revoke":
        try:
            revoked = revoke_ip(args.ip)
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
            raise SystemExit(1)
        print(json.dumps({"ip": args.ip, "revoked": revoked}, ensure_ascii=False))
        return


if __name__ == "__main__":
    _cli()
