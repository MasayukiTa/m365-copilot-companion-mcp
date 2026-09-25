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
# Set before importing so _unlock_password reads the environment rather than .env --
# and REMOVED again at the end of this module (see the atexit hook below).
#
# Leaving it set leaked into whatever pytest imported next in the same process: the
# first turn then carried an unlock preamble, and two unrelated tests that assert the
# turn ends with the goal failed in CI while passing locally, purely on import order.
_PREVIOUS_PW = os.environ.get("MCP_UNLOCK_PASSWORD")
os.environ["MCP_UNLOCK_PASSWORD"] = PW


def _restore_env():
    if _PREVIOUS_PW is None:
        os.environ.pop("MCP_UNLOCK_PASSWORD", None)
    else:
        os.environ["MCP_UNLOCK_PASSWORD"] = _PREVIOUS_PW


# このモジュールは import されるだけで環境を書き換える（下の import より前に
# 立てないと .env を読みに行ってしまうため）。pytest が続けて別のテストを
# import する前に戻す必要があるので、収集後すぐ動くフィクスチャで戻す。
# atexit ではプロセス終了時になり、同じ実行内の後続テストには間に合わない。
try:
    import pytest

    @pytest.fixture(scope="module", autouse=True)
    def _unlock_env():
        os.environ["MCP_UNLOCK_PASSWORD"] = PW
        yield
        _restore_env()
except ImportError:      # 単体スクリプトとして走らせたとき
    pytest = None

# UNLOCK EXHAUSTION NOW RAISES A REAL HITL GATE (operator E, 2026-09-15) instead of only
# setting a status field -- and this file's OWN documented invocation
# (".venv\Scripts\python.exe relay\test_unlock_inject.py") runs OUTSIDE pytest, so
# conftest.py's module-scope MCP_GATE_DIR/MCP_SUPPRESS_GUI redirects (set before pytest
# imports anything) never run. Running this exact cap-exhaustion test standalone, before this
# was added, wrote a real gate file into the operator's live ~/.companion_gates AND spawned a
# real FleetCockpit.exe --approval-gate window on the operator's desktop -- the identical
# failure mode conftest.py's own comment warns about for the pytest path ("378 pending
# questions ... every one naming a pytest temp directory"), reached here by the one entry
# point that skips conftest.py entirely. `setdefault` so a run UNDER pytest (which already set
# these at collection time) is unaffected; only the standalone script path is redirected.
os.environ.setdefault("MCP_SUPPRESS_GUI", "1")
os.environ.setdefault(
    "MCP_GATE_DIR", os.path.join(tempfile.gettempdir(),
                                 "companion_gates_test_unlock_inject_%d" % os.getpid()))

import relay.relay_fleet as rf
from relay.relay_fleet import RelayWorker, MAX_UNLOCK_ATTEMPTS

LOCKED = ("[locked client IP: '203.0.113.7'] Call unlock(password='<password>') first. "
          "The unlock is stored per client IP for 30 days.")

results = []


def check(name, cond):
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


def main():
    # 1. CHANGED 2026-09-25: turn 1 (the fresh worker's very first message, plan_mode=False,
    # which is RelayWorker's default) is now ALWAYS the plain goal, even with a local unlock
    # password configured. M365 Copilot's own safety/DLP filter deterministically refused the
    # old proactive "call unlock with this password" turn 1 every time (confirmed byte-identical
    # refusal across multiple production runs on 2026-09-25), so the doomed send is no longer
    # made at all -- see _initial_job_with_unlock's docstring in relay/relay_fleet.py.
    w = RelayWorker("デスクトップにフォルダ作って", "u0")
    check("preflight_attempt_not_counted", w._unlock_attempts == 0)
    check("preflight_job_has_no_unlock_call", "unlock" not in (w.job or ""))
    check("preflight_job_has_no_password", PW not in (w.job or ""))
    check("preflight_job_has_no_token_language", "unlock_token" not in (w.job or ""))
    check("preflight_job_keeps_goal", "フォルダ作って" in (w.job or ""))

    # A locked reply (the FIRST real refusal, which is exactly the "agent discovers it needs
    # unlock" moment the old proactive send was trying to pre-empt) still injects a bounded
    # retry reactively -- this path (_looks_locked -> _inject_unlock) is unchanged.
    w._decide(LOCKED)
    check("inject_increments", w._unlock_attempts == 1)
    check("inject_job_has_unlock", "unlock" in (w.job or ""))
    check("inject_job_has_password", PW in (w.job or ""))
    check("inject_job_keeps_goal", "フォルダ作って" in (w.job or ""))
    check("inject_not_terminal", w.outcome is None and w.status != "stuck")
    check("reason_no_password_leak", PW not in (w.reason or ""))
    check("inject_job_names_the_token", "unlock_token" in (w.job or ""))
    check("inject_job_says_to_pass_it", "渡して" in (w.job or ""))
    # THE PASSWORD IS ALREADY IN THIS PROMPT -- DO NOT HUNT FOR IT. Workers read .env (and
    # .env.example / .env.defaults.json / .unlock_state.json) looking for a password that the
    # prefix already embeds via %s. The server refuses .env every time, so the hunt never ends:
    # on 2026-09-07 one worker burned 11 turns on it and STUCK. The prefix must say the password
    # is already here, must name .env as the file not to read, and must say read-only work needs
    # no unlock at all. Assert the injected job carries that guidance.
    check("inject_job_says_password_is_here", ("探さない" in (w.job or "")) or ("探しに行かない" in (w.job or "")))
    check("inject_job_says_not_dot_env", ".env" in (w.job or ""))
    check("inject_job_says_readonly_no_unlock", "読み取り" in (w.job or ""))

    # 2. cap: after MAX_UNLOCK_ATTEMPTS injections, the next locked reply -> ask a human
    # (CHANGED 2026-09-15, operator E wired in: exhausting the unlock budget is precisely a
    # question only a person can answer -- MCP_REQUIRE_UNLOCK_TOKEN? a rotating IP? a wrong
    # password? -- so it now raises a HITL gate instead of settling STUCK outright; see
    # relay/test_a_worker_that_needs_a_person_should_say_so.py for that mechanism in depth.
    # It still settles STUCK, unanswered, after GATE_ANSWER_TIMEOUT_S -- not exercised here.)
    # CHANGED 2026-09-25: attempt 0 no longer comes from a proactive preflight, so all
    # MAX_UNLOCK_ATTEMPTS attempts are now spent by MAX_UNLOCK_ATTEMPTS reactive locked
    # replies (previously MAX_UNLOCK_ATTEMPTS - 1, since the preflight itself spent the 1st).
    w2 = RelayWorker("g", "u1")
    for _ in range(MAX_UNLOCK_ATTEMPTS):
        w2._decide(LOCKED)
    check("cap_attempts_reached", w2._unlock_attempts == MAX_UNLOCK_ATTEMPTS)
    w2._decide(LOCKED)                                  # one past the cap
    check("cap_asks_a_human", w2.status == "awaiting_gate" and bool(w2._gate_token))
    check("cap_reason_actionable", "unlock" in (w2.reason or "") and PW not in (w2.reason or ""))
    # THE REASON MUST LIST THE CAUSE THAT HAPPENS. It named a rotating backend IP and a wrong
    # password; on 2026-09-07 it was neither, and the two jobs that hit this spent 17 and 6
    # turns before anyone looked past the reason it printed. Whoever reads it is trying to find
    # out why, so the token-enforcement case belongs in it -- and it is the cheapest to check.
    check("cap_reason_names_token_enforcement", "unlock_token" in (w2.reason or ""))

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

    # 4. A benign reply must not add a reactive attempt (CHANGED 2026-09-25: there is no
    # proactive attempt anymore, so the baseline is 0, not 1).
    w4 = RelayWorker("g", "u3")
    w4._decide("作業を続けています。CONTINUE")
    check("benign_no_extra_unlock", w4._unlock_attempts == 0)

    # 5. The transient password must never be persisted in the local transcript.
    with tempfile.TemporaryDirectory() as td:
        tx = rf._Transcript(td, "unlock-redaction", "u4", "g")
        tx.user(1, 'unlock {"password": "%s"}' % PW)
        transcript_text = Path(tx.path).read_text(encoding="utf-8")
        check("transcript_redacts_password", PW not in transcript_text)
        # 期待していた "<redacted-unlock-password>" を出力するコードは一度も存在せず、
        # 共有 redactor は "<redacted>" を書く。パスワードは確実に消えていた（上の
        # チェック）ので漏洩ではなく期待値の陳腐化。固定すべき性質は「黙って削除
        # されるのではなく、印が残ること」-- 出所の定数を import して比べる。
        from tools.secret_store import REDACTION_MARKER
        check("transcript_has_redaction_marker", REDACTION_MARKER in transcript_text)

    ok = sum(results)
    total = len(results)
    print("\n=== %d/%d auto-unlock checks passed ===" % (ok, total))
    if ok == total:
        print("ALL UNLOCK INJECT TESTS PASSED")
    sys.exit(0 if ok == total else 1)


if __name__ == "__main__":
    main()
