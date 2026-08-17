import contextvars
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
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
    """Whole-file write. Prefer `_update_state` -- see why there."""
    _atomic_write(state)


def _atomic_write(state: dict) -> None:
    """Write via a temp file and os.replace, so a reader never sees a half-written file.

    `_load_state` returns {} on a parse error, and {} means nobody is unlocked. A plain
    `write_text` leaves a window in which every caller is refused.
    """
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(STATE_FILE.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


#: Serialises read-modify-write ON THIS PROCESS. Two unlocks racing used to both read the
#: file, both edit their own copy, and the second write erase the first -- so one client's
#: authorisation disappeared the moment another unlocked. This does not coordinate across
#: PROCESSES; the cockpit CLI runs separately, and a cross-process lock is the next step if
#: that ever races in practice. Stated rather than implied, because "we take a lock" reads
#: like more of a guarantee than this is.
_STATE_LOCK = threading.RLock()


def _update_state(mutate) -> dict:
    """Read, apply `mutate`, and write back as one operation.

    Every writer goes through here so that no two of them can interleave a read with another's
    write. `mutate` receives the loaded dict and returns the dict to persist.
    """
    with _STATE_LOCK:
        state = mutate(_load_state()) or {}
        _atomic_write(state)
        return state


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


#: THE TOKEN PRESENTED BY THE CALL IN FLIGHT, set by the gateway for one invocation. A
#: context variable rather than a parameter because the alternative is an argument on every
#: gated tool, and 116 signatures is not a change anyone can review.
#:
#: A ContextVar, NOT a threading.local -- and the difference is not cosmetic. This server is
#: ASGI. `threading.local` isolates OS threads, not asyncio tasks, so two tasks on the same
#: event-loop thread share one slot: task B sets an empty token and awaits, task A sets its
#: token and awaits, B resumes and the gate reads A's. That authorises B with A's credential,
#: and the `finally` then clears whichever value happens to be there rather than its own.
#:
#: Today every registered tool is synchronous and runs to completion inside a worker thread,
#: so the interleaving cannot occur -- which makes this the kind of defect that is invisible
#: until the day someone adds one async tool, and then is a cross-request authorisation bug.
#: A ContextVar is per-task by construction and costs nothing to use correctly.
_PRESENTED: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "mcp_presented_unlock_token", default="")


def set_presented_token(token: str):
    """Record the token accompanying this call. Returns a handle for `reset_presented_token`."""
    return _PRESENTED.set((token or "").strip())


def reset_presented_token(handle) -> None:
    """Restore whatever this context held before -- the correct counterpart to `set`.

    Resetting to the PREVIOUS value rather than blanking is what keeps a nested call from
    erasing its caller's token.
    """
    try:
        _PRESENTED.reset(handle)
    except (ValueError, LookupError):
        # The handle belongs to another context (a tool that hopped threads). Blanking is the
        # safe direction: a token that outlives its call is the defect this replaces.
        _PRESENTED.set("")


def clear_presented_token() -> None:
    _PRESENTED.set("")


def presented_token() -> str:
    return _PRESENTED.get() or ""


#: How many live tokens one identity may hold at once. Several agents can share an address --
#: one NAT, one tenant egress -- and a single slot means each new unlock evicts the last, so
#: two legitimate clients lock each other out in a loop. Bounded because the list is walked on
#: every gated call and an unbounded one is a slow leak in a hot path.
_MAX_TOKENS_PER_IDENTITY = 8


def _token_matches(entry: dict, presented: str) -> bool:
    """Whether `presented` is one of the tokens issued for this identity.

    Compared against every live hash, in constant time per comparison. An entry written before
    tokens existed has none, and this returns False -- whether that should refuse the call is
    `enforce_unlock_token`'s decision, not this function's.
    """
    if not presented:
        return False
    entry = entry or {}
    stored = [h for h in (entry.get("token_hashes") or []) if isinstance(h, str)]
    if entry.get("token_sha256"):
        stored.append(entry["token_sha256"])
    if not stored:
        return False
    candidate = hashlib.sha256(presented.encode("utf-8")).hexdigest()
    # `any` over compare_digest rather than `candidate in stored`: the membership test on
    # strings short-circuits on the first differing character, which is the thing
    # compare_digest exists to avoid.
    return any(hmac.compare_digest(h, candidate) for h in stored)


def enforce_unlock_token() -> bool:
    """Whether a matching token is REQUIRED, or merely recorded when present.

    Default off, and deliberately so. Requiring it is the fix; requiring it before anyone has
    unlocked under the new scheme would lock out every existing session at once, including
    unattended ones, and an outage is how a security change gets reverted wholesale instead of
    kept. Turn it on -- MCP_REQUIRE_UNLOCK_TOKEN=1 -- once the operators have re-unlocked.

    While it is off the gate is exactly as weak as it was, and `token_ok` on every refusal
    record says whether the call WOULD have passed, so the switch can be flipped on evidence
    rather than on hope.
    """
    return os.environ.get("MCP_REQUIRE_UNLOCK_TOKEN") == "1"


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
        # THE IP GOT US THIS FAR; THE TOKEN IS WHAT MAKES IT A SECOND KEY. The IP came out of
        # a header the caller controls, so on its own it proves possession of the API key and
        # nothing else. A token was issued to whoever supplied the password, and only its hash
        # was kept.
        entry = (_load_state() or {}).get(ip) or {}
        ok = _token_matches(entry, presented_token())
        if ok or not enforce_unlock_token():
            if not ok:
                # Recorded, not enforced: this is the number that says whether enforcement can
                # be switched on without an outage.
                lock_state.record_token_gap(ip)
            return None
        msg = (
            f"[locked: no valid unlock token for {ip!r}] The identity in the forwarding "
            "header is not sufficient on its own. Call unlock(password='<password>') and "
            "pass the returned `unlock_token` with the call."
        )
        lock_state.record_locked(ip, msg)
        return msg
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
    # A SECOND FACTOR THAT IS ACTUALLY A SECOND FACTOR.
    #
    # The unlock record was keyed on an IP, and an IP is a value a caller STATES rather than
    # proves: it is derived from a forwarding header, and on this deployment nothing upstream
    # appends its own hop, so the whole header is caller-supplied. Anyone holding the API key
    # could therefore assert an unlocked identity and be believed. Two keys collapsed into one.
    #
    # The token fixes that because it is issued rather than asserted: it exists only in the
    # reply to a correct password, and only its hash is stored, so the state file is no longer
    # a list of things to impersonate. The IP stays in the record for auditing and for the
    # grace path below; it is no longer sufficient on its own once a token has been issued.
    # SEVERAL TOKENS PER IDENTITY, because an identity is not a client. Two agents behind one
    # NAT or one tenant egress present the SAME address: with a single stored hash, the second
    # to unlock silently invalidated the first, and each would keep locking the other out
    # forever. The identity is a namespace; the tokens are the credentials in it.
    token = secrets.token_urlsafe(24)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _add(state):
        entry = dict(state.get(ip) or {})
        hashes = [h for h in (entry.get("token_hashes") or []) if isinstance(h, str)]
        # Carry a pre-multi-token entry across without losing it.
        if entry.get("token_sha256") and entry["token_sha256"] not in hashes:
            hashes.append(entry["token_sha256"])
        hashes.append(digest)
        entry.update({
            "expires_at": max(float(entry.get("expires_at") or 0), expires),
            "unlocked_at": time.time(),
            # BOUNDED. Every unlock adds one; without a cap the file grows forever and each
            # comparison walks all of it. The oldest go first -- they are the ones whose
            # holders have already re-unlocked.
            "token_hashes": hashes[-_MAX_TOKENS_PER_IDENTITY:],
        })
        entry.pop("token_sha256", None)
        state[ip] = entry
        return state

    _update_state(_add)
    # The refusal that prompted this unlock is now history; drop it so a reader
    # checking "was a call just refused?" is not answered by a stale record.
    lock_state.clear()
    return (
        f"Unlocked IP {ip!r} for {ttl_days} days.\n"
        f"unlock_token: {token}\n"
        "Pass this as `unlock_token` on call_tool for mutating and execution tools. "
        "It is shown once and only its hash is kept."
    )


def _format_entry(ip: str, entry: dict, now: float) -> str:
    remain_days = (entry.get("expires_at", 0) - now) / 86400
    return f"{ip}: expired" if remain_days <= 0 else f"{ip}: {remain_days:.1f} days remaining"


def list_unlocked() -> str:
    """Whether the CALLER is unlocked. Not a directory of everyone else.

    IT USED TO RETURN THE WHOLE TABLE. This tool is registered as an ordinary MCP tool and is
    not itself behind the unlock gate, so it handed any caller the full list of the identities
    the authorisation check keys on. Whatever else is true of that check, a tool that
    enumerates its keys for an unauthorised caller is doing part of the work for them.
    (Raised in an external review and reproduced against this code, 2026-08-17.)

    Removing the tool outright would have broken a real use: `bench/swe_unlock_bootstrap.py`
    unlocks and then asks whether it worked. That question is about the CALLER, and answering
    only that discloses nothing -- a caller who is already the identity in question learns
    nothing it did not have. Everyone else's entries are not the caller's business.

    A NARROWING, NOT A FIX. See tests/test_unlock_oracle.py, which asserts what is still
    open. The consequences are not spelled out in this repository, which is public.

    A genuine local caller (loopback peer, no forwarding header) still gets the full table:
    that is the operator at the machine, and the same information is on their own disk.
    """
    state = _load_state()
    now = time.time()
    try:
        is_local, ip = _parse_request(get_http_request())
    except Exception:
        # No HTTP context: the CLI / cockpit path, which is the operator on this machine.
        return "\n".join(_format_entry(k, v, now) for k, v in state.items()) or \
            "(no unlocked remote IPs)"
    if is_local:
        return "\n".join(_format_entry(k, v, now) for k, v in state.items()) or \
            "(no unlocked remote IPs)"
    entry = state.get(ip)
    if not entry:
        return f"{ip}: not unlocked"
    return _format_entry(ip, entry, now)


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
    # A TOKEN HERE TOO, OR THIS PATH BECOMES THE BYPASS.
    #
    # `unlock()` issues one; this admin path did not. Under MCP_REQUIRE_UNLOCK_TOKEN=1 that
    # leaves two bad outcomes and no good one: either an entry with no token is refused --
    # locking out a client the operator deliberately authorised, who has no way to obtain a
    # token because they never called unlock() -- or entries without tokens are accepted,
    # which is the hole with extra steps.
    #
    # So the token is minted here and RETURNED to the operator, whose job it then is to hand
    # it to the client they just authorised. That is a real manual step and it is the correct
    # one: this function exists precisely for the case where a human vouches for a machine
    # that cannot present the password itself.
    token = secrets.token_urlsafe(24)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.time()
    expires = now + ttl_days * 86400

    def _add(state):
        # ADDED TO THE IDENTITY'S TOKENS, not written over them. Vouching for one more client
        # behind a shared address must not evict the ones already there -- that was a second
        # way for two legitimate clients to lock each other out.
        entry = dict(state.get(ip) or {})
        hashes = [h for h in (entry.get("token_hashes") or []) if isinstance(h, str)]
        if entry.get("token_sha256") and entry["token_sha256"] not in hashes:
            hashes.append(entry["token_sha256"])
        hashes.append(digest)
        entry.update({"expires_at": max(float(entry.get("expires_at") or 0), expires),
                      "unlocked_at": now, "granted_by": "cockpit",
                      "token_hashes": hashes[-_MAX_TOKENS_PER_IDENTITY:]})
        entry.pop("token_sha256", None)
        state[ip] = entry
        return state

    _update_state(_add)
    return {"ip": ip, "expires_at": expires, "unlocked_at": now, "ttl_days": ttl_days,
            "unlock_token": token}


def revoke_ip(ip: str) -> bool:
    """LOCAL operator action: remove any unlock for `ip`. Returns True if an entry was
    removed, False if `ip` had no entry (a no-op, not an error). Raises ValueError for an
    empty/garbage IP."""
    ip = (ip or "").strip()
    if not _valid_ip(ip):
        raise ValueError(f"invalid IP address: {ip!r}")
    # ONE LOCK, NOT TWO. `_GRANT_LOCK` guarded the cockpit paths and nothing guarded unlock(),
    # so the two could interleave and lose each other's writes -- two locks that do not
    # exclude each other are no lock at all for the pair.
    seen = {"existed": False}

    def _drop(state):
        seen["existed"] = ip in state
        state.pop(ip, None)
        return state

    _update_state(_drop)
    return seen["existed"]


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
