"""Child-process side of tests/test_unlock_revocation.py's grant-vs-revoke races.

A separate module (not the test file) so multiprocessing's spawn start method can import the
target by name. Every path the children touch is redirected into the test's temp directory
BEFORE any write -- the live .unlock_state.json and .fleet/ ledgers are never opened.
"""
import json
import os
import tempfile
import time
from pathlib import Path


def x_ip(i: int) -> str:
    """The identity revoked in iteration i (RFC 5737 documentation range)."""
    return "203.0.113.%d" % (i + 1)


def y_ip(i: int) -> str:
    """The identity granted in iteration i."""
    return "198.51.100.%d" % (i + 1)


def _redirect(tmp: str):
    from tools import lock_state
    from tools import security as S

    S.STATE_FILE = Path(tmp) / "unlock.json"
    lock_state._STATE_FILE = Path(tmp) / "lock.json"
    lock_state._LOG_FILE = Path(tmp) / "lock_refusals.jsonl"
    lock_state._TOKEN_GAP_FILE = Path(tmp) / "gap.json"
    return S


def _touch_if_present(S, ip: str, sess: str) -> None:
    """The server's session refresh, reduced to its write: bind a session to the identity's
    newest grant if -- at the time of the write -- the identity still has one."""
    def _add(state, tx):
        e = state.get(ip)
        if not e or not e.get("grants"):
            return
        gid = S._grant_order(e["grants"])[-1]
        state[ip] = S._touch_session(e, sess, tx.now, gid)
    S._transact(_add)


def _stale_flat_write(S, ip: str, raw_before: dict, sess: str) -> None:
    """What the PREVIOUS version of tools/security.py did in another process: take a copy read
    earlier, edit one identity, replace the whole file -- no cross-process lock, no ledger."""
    state = json.loads(json.dumps(raw_before))
    e = dict(state.get(ip) or {})
    sessions = dict(e.get("sessions") or {})
    sessions[sess] = time.time()
    e["sessions"] = sessions
    state[ip] = e
    fd, tmp = tempfile.mkstemp(dir=str(S.STATE_FILE.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    for _ in range(400):
        try:
            os.replace(tmp, S.STATE_FILE)
            return
        except PermissionError:
            time.sleep(0.005)
    raise RuntimeError("stale writer could not replace the state file")


def granter(tmp: str, n: int, barrier) -> None:
    """Per iteration: grant a fresh identity Y_i, and refresh a session on X_i -- the identity
    the other process is revoking at the same moment."""
    S = _redirect(tmp)
    for i in range(n):
        barrier.wait()
        S.grant_ip(y_ip(i), ttl_days=1)
        _touch_if_present(S, x_ip(i), "sess-%d" % i)


def revoker(tmp: str, n: int, barrier) -> None:
    S = _redirect(tmp)
    for i in range(n):
        barrier.wait()
        S.revoke_ip(x_ip(i))


def stale_writer(tmp: str, n: int, barrier) -> None:
    """Old-writer race, made deterministic: read BEFORE the revoke, write AFTER it."""
    S = _redirect(tmp)
    for i in range(n):
        barrier.wait()                       # 1: both at the start of iteration i
        try:
            raw = json.loads(S.STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        barrier.wait()                       # 2: stale copy taken; revoker may go
        barrier.wait()                       # 3: revoker has finished
        _stale_flat_write(S, x_ip(i), raw, "stale-%d" % i)


def revoker_for_stale(tmp: str, n: int, barrier) -> None:
    S = _redirect(tmp)
    for i in range(n):
        barrier.wait()
        barrier.wait()
        S.revoke_ip(x_ip(i))
        barrier.wait()
