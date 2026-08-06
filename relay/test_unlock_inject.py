"""Tests for the relay's AUTO-UNLOCK injection (write/exec tools hit a locked client IP).

The MCP server gates mutating tools behind unlock(password) per client IP. When the agent
calls one before the (rotating) M365 backend IP is unlocked, the server returns
"[locked client IP: ...] Call unlock(password=...) first." which the agent echoes. The relay
detects this and AUTO-INJECTS a turn that calls the unlock tool with MCP_UNLOCK_PASSWORD read
LOCALLY from .env -- it is NEVER baked into the agent's persistent Copilot Studio instructions
(that would expose the password permanently). Bounded by MAX_UNLOCK_ATTEMPTS (the backend IP
can rotate and re-lock).

Run:  .venv\\Scripts\\python.exe relay\\test_unlock_inject.py
"""
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

PW = "unit_test_pw_abc123"
os.environ["MCP_UNLOCK_PASSWORD"] = PW   # set BEFORE import so _unlock_password reads env, not .env

import relay.relay_fleet as rf
from relay.relay_fleet import RelayWorker, MAX_UNLOCK_ATTEMPTS

LOCKED = ("[locked client IP: '203.0.113.7'] Call unlock(password='<password>') first. "
          "The unlock is stored per client IP for 30 days.")

results = []


def check(name, cond):
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


def main():
    # 1. The first turn proactively unlocks before tool discovery / task execution.
    w = RelayWorker("デスクトップにフォルダ作って", "u0")
    check("preflight_attempt_counted", w._unlock_attempts == 1)
    check("preflight_job_has_unlock", "unlock" in (w.job or ""))
    check("preflight_job_has_password", PW in (w.job or ""))
    check("preflight_job_keeps_goal", "フォルダ作って" in (w.job or ""))

    # A later locked reply (for example after backend IP rotation) still injects a bounded retry.
    w._decide(LOCKED)
    check("inject_increments", w._unlock_attempts == 2)
    check("inject_job_has_unlock", "unlock" in (w.job or ""))
    check("inject_job_has_password", PW in (w.job or ""))
    check("inject_job_keeps_goal", "フォルダ作って" in (w.job or ""))
    check("inject_not_terminal", w.outcome is None and w.status != "stuck")
    check("reason_no_password_leak", PW not in (w.reason or ""))

    # 2. cap: after MAX_UNLOCK_ATTEMPTS injections, the next locked reply -> STUCK (no infinite loop)
    w2 = RelayWorker("g", "u1")
    for _ in range(MAX_UNLOCK_ATTEMPTS - 1):
        w2._decide(LOCKED)
    check("cap_attempts_reached", w2._unlock_attempts == MAX_UNLOCK_ATTEMPTS)
    w2._decide(LOCKED)                                  # one past the cap
    check("cap_goes_stuck", w2.status == "stuck" and w2.outcome == "STUCK")
    check("cap_reason_actionable", "unlock" in (w2.reason or "") and PW not in (w2.reason or ""))

    # 3. missing password -> STUCK with a clear 'not configured' reason (patch the local reader)
    orig = rf._unlock_password
    rf._unlock_password = lambda: ""
    try:
        w3 = RelayWorker("g", "u2")
        w3._decide(LOCKED)
        check("nopw_goes_stuck", w3.status == "stuck" and w3.outcome == "STUCK")
        check("nopw_reason_mentions_env", "MCP_UNLOCK_PASSWORD" in (w3.reason or ""))
    finally:
        rf._unlock_password = orig

    # 4. A benign reply must not add a reactive attempt beyond the proactive one.
    w4 = RelayWorker("g", "u3")
    w4._decide("作業を続けています。CONTINUE")
    check("benign_no_extra_unlock", w4._unlock_attempts == 1)

    # 5. The transient password must never be persisted in the local transcript.
    with tempfile.TemporaryDirectory() as td:
        tx = rf._Transcript(td, "unlock-redaction", "u4", "g")
        tx.user(1, 'unlock {"password": "%s"}' % PW)
        transcript_text = Path(tx.path).read_text(encoding="utf-8")
        check("transcript_redacts_password", PW not in transcript_text)
        check("transcript_has_redaction_marker", "<redacted-unlock-password>" in transcript_text)

    ok = sum(results)
    total = len(results)
    print("\n=== %d/%d auto-unlock checks passed ===" % (ok, total))
    if ok == total:
        print("ALL UNLOCK INJECT TESTS PASSED")
    sys.exit(0 if ok == total else 1)


if __name__ == "__main__":
    main()
