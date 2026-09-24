"""SEC-02 and SEC-03: the unlock gate's second factor by default, and revocations that stay put.

SEC-02. An unlocked identity comes from a forwarding header the caller controls. With
MCP_REQUIRE_UNLOCK_TOKEN off (the old default) a caller holding only the API key could state the
address of a client that had unlocked and be let through. Enforcement is now the default: the
call must carry the token unlock() issued, or arrive in a session recorded at unlock.

SEC-03. The server and the cockpit's `python -m tools.security revoke` are different processes
and both did read-modify-replace on .unlock_state.json under a lock only one of them held, so a
server write that read the table just before a revoke put the revoked identity back just after.
Grants now have ids, revocations write tombstones to a ledger no stale writer knows about, and
every write re-reads both under a cross-process lock.

The races below run in REAL separate processes against a temp copy of the table, 200 iterations
each, with barriers so both sides start every iteration together.
"""
import hashlib
import json
import multiprocessing
import time

import pytest

from tools import lock_state
from tools import security as S

import _unlock_race_worker as W   # tests/ is on sys.path (pytest rootdir-relative import)

IP = "203.0.113.77"       # RFC 5737 documentation range


class _Req:
    def __init__(self, peer="127.0.0.1", xff=IP):
        self.client = type("c", (), {"host": peer})()
        self.headers = {"x-forwarded-for": xff} if xff else {}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "STATE_FILE", tmp_path / "unlock.json")
    monkeypatch.setattr(lock_state, "_STATE_FILE", tmp_path / "lock.json")
    monkeypatch.setattr(lock_state, "_LOG_FILE", tmp_path / "lock_refusals.jsonl")
    monkeypatch.setattr(lock_state, "_TOKEN_GAP_FILE", tmp_path / "gap.json")
    monkeypatch.setattr(S, "get_http_request", lambda: _Req())
    monkeypatch.setattr(S, "unlock_password_from_env", lambda: "pw")
    monkeypatch.delenv("MCP_REQUIRE_UNLOCK_TOKEN", raising=False)   # THE DEFAULT
    monkeypatch.delenv("MCP_UNLOCK_SESSION_AUTH", raising=False)
    S.clear_presented_token()
    yield
    S.clear_presented_token()


def _session(monkeypatch, sid):
    monkeypatch.setattr(S, "_current_session_fingerprint", lambda: sid)


def _token(out: str) -> str:
    return [l.split(": ", 1)[1] for l in out.splitlines() if l.startswith("unlock_token: ")][0]


def _call(monkeypatch, *, xff=IP, sess="", token=""):
    """One gated call as the gateway makes it: identity from the header, session from the
    transport, token from the arguments."""
    monkeypatch.setattr(S, "get_http_request", lambda: _Req(xff=xff))
    _session(monkeypatch, sess)
    S.set_presented_token(token)
    try:
        return S.require_unlocked()
    finally:
        S.clear_presented_token()


# ── SEC-02: the second factor is required by default ─────────────────────────────────────

def test_a_forged_identity_without_token_or_session_is_refused_by_default(monkeypatch):
    """THE ATTACK: an API-key holder states the address of a client that did unlock."""
    _session(monkeypatch, "victim-session")
    S.unlock("pw")
    refusal = _call(monkeypatch, sess="attacker-session")
    assert refusal is not None
    assert refusal.startswith("[locked: no valid unlock token for '%s']" % IP)
    refusal = _call(monkeypatch, sess="")
    assert refusal is not None, "no session at all must not fall back to the identity"
    assert lock_state.token_gap().get("count", 0) == 0


def test_the_refusal_says_exactly_what_to_do_and_stays_short(monkeypatch):
    _session(monkeypatch, "s1")
    S.unlock("pw")
    refusal = _call(monkeypatch, xff="198.51.100.200")   # an address never unlocked
    assert refusal.startswith("[locked client IP")
    refusal = _call(monkeypatch, sess="other")
    assert "again now, in this conversation" in refusal
    assert "unlock_token" in refusal and "fleet_submit" in refusal
    # relay_fleet classifies a lock refusal only while it dominates a short reply.
    long_ip = "2001:db8:ffff:ffff:ffff:ffff:ffff:ffff"
    monkeypatch.setattr(S, "get_http_request", lambda: _Req(xff=long_ip))
    _session(monkeypatch, "s-long")
    S.unlock("pw")
    assert len(_call(monkeypatch, xff=long_ip, sess="nope")) < 400


def test_the_token_path_passes(monkeypatch):
    _session(monkeypatch, "")
    tok = _token(S.unlock("pw"))
    assert _call(monkeypatch, token=tok) is None
    assert _call(monkeypatch, token="not-the-token") is not None


def test_the_session_path_passes(monkeypatch):
    _session(monkeypatch, "conv-1")
    S.unlock("pw")
    assert _call(monkeypatch, sess="conv-1") is None


def test_a_fleet_worker_shaped_flow_passes(monkeypatch):
    """The relay injects unlock(password) into a worker's first turn; the worker calls it in
    its session, keeps the token, and works on. Measured on the production host: a new turn can
    arrive under a NEW Mcp-Session-Id, so the token is what carries it there."""
    _session(monkeypatch, "turn-1")
    tok = _token(S.unlock("pw"))
    assert _call(monkeypatch, sess="turn-1") is None, "same session, token dropped"
    assert _call(monkeypatch, sess="turn-1", token=tok) is None
    assert _call(monkeypatch, sess="turn-2", token=tok) is None, "new session, token kept"
    assert _call(monkeypatch, sess="turn-2") is None, (
        "the token-matched call must establish the new session for the calls after it")
    refusal = _call(monkeypatch, sess="turn-3")
    assert refusal is not None and "again now, in this conversation" in refusal
    assert lock_state.token_gap().get("count", 0) == 0, "the gap must be zero on this flow"


def test_a_cockpit_grant_is_usable_with_its_token_only(monkeypatch):
    tok = S.grant_ip("198.51.100.9", ttl_days=1)["unlock_token"]
    assert _call(monkeypatch, xff="198.51.100.9", sess="x", token=tok) is None
    assert _call(monkeypatch, xff="198.51.100.9", sess="x") is None, "session bound by the token"
    assert _call(monkeypatch, xff="198.51.100.9", sess="z") is not None


def test_the_template_and_bootstrap_write_the_enforcing_value():
    """New installs get the line; existing ones get it from bootstrap's template backfill."""
    import pathlib
    import sys

    root = pathlib.Path(__file__).resolve().parent.parent
    example = (root / ".env.example").read_text(encoding="utf-8")
    assert "\nMCP_REQUIRE_UNLOCK_TOKEN=1\n" in example
    sys.path.insert(0, str(root / "scripts"))
    try:
        import bootstrap
    finally:
        sys.path.pop(0)
    lines, _ = bootstrap.missing_template_lines("MCP_API_KEY=x\n", example)
    assert "MCP_REQUIRE_UNLOCK_TOKEN=1" in lines
    lines, _ = bootstrap.missing_template_lines("MCP_API_KEY=x\n", None)
    assert "MCP_REQUIRE_UNLOCK_TOKEN=1" in lines, "fallback defaults lack it"
    lines, _ = bootstrap.missing_template_lines("MCP_REQUIRE_UNLOCK_TOKEN=0\n", example)
    assert not any(l.startswith("MCP_REQUIRE_UNLOCK_TOKEN") for l in lines), \
        "an operator's explicit 0 must not be overwritten"


# ── SEC-03: grants have ids; revocations stay revoked ─────────────────────────────────────

def _raw():
    return json.loads(S.STATE_FILE.read_text(encoding="utf-8"))


def test_every_grant_has_its_own_id_and_generation(monkeypatch):
    _session(monkeypatch, "a")
    S.unlock("pw")
    S.unlock("pw")
    grants = _raw()[IP]["grants"]
    assert len(grants) == 2
    gens = sorted(g["generation"] for g in grants.values())
    assert gens[0] < gens[1]
    assert all(len(gid) == 32 for gid in grants)       # uuid4 hex


def test_revoke_kills_tokens_and_sessions_and_a_later_grant_still_works(monkeypatch):
    _session(monkeypatch, "old-conv")
    tok = _token(S.unlock("pw"))
    assert S.revoke_ip(IP) is True
    assert _call(monkeypatch, token=tok) is not None
    assert _call(monkeypatch, sess="old-conv") is not None
    # THE SAME CLOCK TICK as the revocation (a coarse Windows clock does this for real):
    # ordering must come from the generation, not from time.
    frozen = json.loads(S._revocations_file().read_text(encoding="utf-8"))[
        "identities"][IP]["revoked_at"]
    monkeypatch.setattr(S.time, "time", lambda: frozen)
    _session(monkeypatch, "new-conv")
    tok2 = _token(S.unlock("pw"))
    assert _call(monkeypatch, token=tok2) is None, "a grant after a revocation was lost"
    assert _call(monkeypatch, token=tok) is not None, "the revoked token came back"
    assert _call(monkeypatch, sess="old-conv") is not None, "the revoked session came back"


def test_a_revoked_session_can_be_unlocked_again_by_the_password(monkeypatch):
    """The tombstone names the session; it must not outlive the grants it revoked. The same
    conversation unlocking again after the revoke is a new grant, and its session counts."""
    _session(monkeypatch, "conv")
    S.unlock("pw")
    S.revoke_ip(IP)
    assert _call(monkeypatch, sess="conv") is not None
    _session(monkeypatch, "conv")
    S.unlock("pw")
    assert _call(monkeypatch, sess="conv") is None, "re-unlock in the revoked session was lost"
    assert _call(monkeypatch, sess="other") is not None


def test_the_generation_never_repeats_when_a_record_is_removed_outside_the_lock(monkeypatch):
    """A writer outside the lock (the pre-change server) can drop the grant that held the
    highest generation; the next one handed out must still be higher."""
    S.grant_ip("198.51.100.1", ttl_days=1)
    S.grant_ip("198.51.100.2", ttl_days=1)
    top = max(g["generation"] for g in _raw()["198.51.100.2"]["grants"].values())
    raw = _raw()
    del raw["198.51.100.2"]
    S._save_state_atomic(raw)                          # whole-file replace, no lock
    S.grant_ip("198.51.100.3", ttl_days=1)
    nxt = max(g["generation"] for g in _raw()["198.51.100.3"]["grants"].values())
    assert nxt > top, "generation %r handed out again after %r" % (nxt, top)


def test_a_stale_copy_written_back_after_a_revoke_does_not_resurrect(monkeypatch):
    """The old writer, in-process for determinism: read, (revoke happens), write back."""
    _session(monkeypatch, "c1")
    tok = _token(S.unlock("pw"))
    stale = _raw()
    S.revoke_ip(IP)
    S._save_state_atomic(stale)                     # whole-file replace, no lock, no ledger
    assert not S.is_unlocked(IP)
    assert _call(monkeypatch, token=tok) is not None
    assert _call(monkeypatch, sess="c1") is not None
    S.grant_ip("198.51.100.1", ttl_days=1)          # any new-code write purges it
    assert IP not in _raw()


def test_a_stale_flat_copy_with_a_new_old_style_unlock_keeps_only_the_new_token(monkeypatch):
    """The pre-change server can also UNLOCK on top of a stale flat copy: its new token must
    work, the revoked ones in the same list must not."""
    old = "old-token"
    S.STATE_FILE.write_text(json.dumps({IP: {
        "expires_at": time.time() + 86400, "unlocked_at": time.time() - 60,
        "token_hashes": [hashlib.sha256(old.encode()).hexdigest()],
        "sessions": {"old-conv": time.time()}}}), encoding="utf-8")
    assert S.revoke_ip(IP) is True
    new = "new-token"
    S.STATE_FILE.write_text(json.dumps({IP: {       # what the old unlock() writes after it
        "expires_at": time.time() + 86400, "unlocked_at": time.time() + 1,
        "token_hashes": [hashlib.sha256(old.encode()).hexdigest(),
                         hashlib.sha256(new.encode()).hexdigest()],
        "sessions": {"old-conv": time.time()}}}), encoding="utf-8")
    assert _call(monkeypatch, token=new) is None
    assert _call(monkeypatch, token=old) is not None
    assert _call(monkeypatch, sess="old-conv") is not None


def test_tombstones_are_bounded(monkeypatch):
    monkeypatch.setenv("MCP_UNLOCK_TTL_DAYS", "1")
    S.grant_ip(IP, ttl_days=1)
    S.revoke_ip(IP)
    ledger = json.loads(S._revocations_file().read_text(encoding="utf-8"))
    assert IP in ledger["identities"] and len(ledger["grants"]) == 1
    later = time.time() + 3 * 86400
    monkeypatch.setattr(S.time, "time", lambda: later)
    S.grant_ip("198.51.100.2", ttl_days=1)
    ledger = json.loads(S._revocations_file().read_text(encoding="utf-8"))
    assert ledger["identities"] == {} and ledger["grants"] == {}


def test_an_unreadable_ledger_fails_closed(monkeypatch):
    tok = S.grant_ip(IP, ttl_days=1)["unlock_token"]
    S._revocations_file().parent.mkdir(parents=True, exist_ok=True)
    S._revocations_file().write_text("{not json", encoding="utf-8")
    assert _call(monkeypatch, token=tok) is not None, "unreadable ledger must not authorise"
    with pytest.raises(RuntimeError):
        S.grant_ip("198.51.100.3", ttl_days=1)


def test_the_live_state_shape_is_read_as_before(monkeypatch):
    """A flat entry as written before grant ids (token list, sessions, cockpit marker) still
    authorises by token and by session, and the first new-code write converts it in place."""
    toks = ["t%d" % i for i in range(3)]
    S.STATE_FILE.write_text(json.dumps({IP: {
        "expires_at": time.time() + 86400, "unlocked_at": time.time() - 10,
        "granted_by": "cockpit",
        "token_hashes": [hashlib.sha256(t.encode()).hexdigest() for t in toks],
        "sessions": {"live-conv": time.time()}}}), encoding="utf-8")
    for t in toks:
        assert _call(monkeypatch, token=t) is None
    assert _call(monkeypatch, sess="live-conv") is None
    S.grant_ip("198.51.100.4", ttl_days=1)
    e = _raw()[IP]
    assert len(e["grants"]) == 3 and e["granted_by"] == "cockpit"
    assert set(e["session_grants"]) == {"live-conv"}
    for t in toks:
        assert _call(monkeypatch, token=t) is None
    assert _call(monkeypatch, sess="live-conv") is None


# ── SEC-03, the races, in real processes ──────────────────────────────────────────────────

N = 200


def _run(tmp_path, a, b):
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    procs = [ctx.Process(target=a, args=(str(tmp_path), N, barrier)),
             ctx.Process(target=b, args=(str(tmp_path), N, barrier))]
    for p in procs:
        p.start()
    for p in procs:
        p.join(300)
    for p in procs:
        assert p.exitcode == 0, "a racing process failed (exit %r)" % p.exitcode


def test_grant_and_revoke_racing_in_two_processes(tmp_path, monkeypatch):
    """Zero resurrections: every X_i revoked stays revoked though the other process wrote a
    session onto X_i in the same instant. Zero lost grants: every Y_i granted by one process
    survives the other process's concurrent revoke-write."""
    for i in range(N):
        S.grant_ip(W.x_ip(i), ttl_days=1)
    _run(tmp_path, W.granter, W.revoker)
    state = S._load_state()
    raw = _raw()
    resurrected = [W.x_ip(i) for i in range(N) if W.x_ip(i) in state or W.x_ip(i) in raw]
    lost = [W.y_ip(i) for i in range(N) if not S.is_unlocked(W.y_ip(i))]
    assert resurrected == [], "revoked identities came back: %r" % resurrected[:5]
    assert lost == [], "grants were lost: %d of %d, e.g. %r" % (len(lost), N, lost[:5])


def test_a_stale_writer_in_another_process_cannot_resurrect(tmp_path, monkeypatch):
    """The pre-change server's read-modify-replace, forced into the worst interleaving on every
    one of 200 iterations: its copy is read before the revoke and written after it."""
    tokens = {W.x_ip(i): S.grant_ip(W.x_ip(i), ttl_days=1)["unlock_token"] for i in range(N)}
    _run(tmp_path, W.stale_writer, W.revoker_for_stale)
    raw = _raw()
    assert sum(1 for i in range(N) if W.x_ip(i) in raw) > 0, (
        "the stale writer never wrote a revoked identity back -- the race was not exercised")
    state = S._load_state()
    resurrected = [ip for ip in tokens if ip in state]
    assert resurrected == [], "revoked identities came back: %r" % resurrected[:5]
    for ip, tok in list(tokens.items())[::20]:
        assert _call(monkeypatch, xff=ip, token=tok) is not None
