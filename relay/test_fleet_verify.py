"""Deterministic tests for the fleet's acceptance GATE (RelayWorker verification).

No browser: we drive the worker's verification sub-machine directly with real (fast)
local checks. Proves the spec 3-3 gate behaviour:

  * no checks            -> DONE is trusted (back-compat), verified=False
  * passing check        -> DONE accepted, verified=True
  * failing check        -> ground truth re-injected (VERIFY_FIX_JOB), status back to
                            ready, attempt counted
  * repeated failure     -> STUCK with outcome VERIFY_FAILED at max_verify_attempts
  * multi-check          -> stops at the first failing check, re-injects ITS detail
  * goal_fields          -> str and dict goal shapes normalize correctly
  * exhaustion salvage   -> at max_turns / per-turn-timeout, if the checks ALREADY pass the
                            worker ends DONE+verified instead of MAXTURNS/STUCK (HumanEval_56
                            class); a failing check still goes MAXTURNS/STUCK (no false salvage)
                            and a no-checks goal is unchanged (back-compat)

Run:  .venv\\Scripts\\python.exe relay\\test_fleet_verify.py
"""
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from relay.relay_fleet import RelayWorker, TERMINAL, goal_fields

PY = sys.executable
results = []


def check(name, cond):
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


def drive_to_settle(w, max_steps=400):
    """Poll the worker until it leaves 'verifying' (terminal, or back to ready)."""
    for _ in range(max_steps):
        if w.status not in ("verifying",):
            return
        w.poll()
        time.sleep(0.01)


PASS_CHECK = {"type": "shell", "argv": [PY, "-c", "print('ok')"]}
FAIL_CHECK = {"type": "shell", "argv": [PY, "-c", "import sys;sys.stderr.write('BOOM');sys.exit(1)"]}


def main():
    # 1. no checks -> DONE trusted, verified=False
    w = RelayWorker("plain goal", "w0")
    w._decide("everything complete DONE")
    check("nocheck_done_trusted", w.status == "done" and w.outcome == "DONE" and w.verified is False)

    # 2. passing check -> verifying -> DONE verified
    w = RelayWorker({"text": "g", "checks": [PASS_CHECK]}, "w1")
    w._decide("all complete DONE")
    check("pass_enters_verifying", w.status == "verifying")
    drive_to_settle(w)
    check("pass_done_verified", w.status == "done" and w.outcome == "DONE" and w.verified is True)

    # 3. failing check -> ground truth re-injected, back to ready, attempt counted
    w = RelayWorker({"text": "g", "checks": [FAIL_CHECK]}, "w2", max_verify_attempts=3)
    w._decide("DONE")
    drive_to_settle(w)
    check("fail_reinject_ready", w.status == "ready" and w.verify_attempts == 1)
    check("fail_job_has_truth", "検証結果" in w.job and "BOOM" in w.job)
    check("fail_not_verified", w.verified is False)

    # 4. repeated failure -> STUCK / VERIFY_FAILED at the cap
    w = RelayWorker({"text": "g", "checks": [FAIL_CHECK]}, "w3", max_verify_attempts=2)
    w._decide("DONE")                 # attempt 1 (re-inject)
    drive_to_settle(w)
    check("cap_first_reinject", w.status == "ready" and w.verify_attempts == 1)
    w._on_done_claimed()             # agent claims DONE again -> attempt 2 hits the cap
    drive_to_settle(w)
    check("cap_verify_failed", w.status == "stuck" and w.outcome == "VERIFY_FAILED"
          and w.status in TERMINAL)

    # 5. multi-check: first passes, second fails -> re-inject SECOND check's detail
    w = RelayWorker({"text": "g", "checks": [PASS_CHECK, FAIL_CHECK]}, "w4")
    w._decide("DONE")
    drive_to_settle(w)
    check("multi_stops_at_fail", w.status == "ready" and "BOOM" in w.job)

    # 6. multi-check all pass -> DONE verified
    w = RelayWorker({"text": "g", "checks": [PASS_CHECK, PASS_CHECK]}, "w5")
    w._decide("DONE")
    drive_to_settle(w)
    check("multi_all_pass", w.status == "done" and w.verified is True)

    # 7. goal_fields shapes
    t, c, cwd = goal_fields("hello")
    check("gf_str", t == "hello" and c == [] and cwd is None)
    t, c, cwd = goal_fields({"goal": "g2", "check": {"type": "shell", "cmd": "x"}, "cwd": "C:/p"})
    check("gf_dict", t == "g2" and len(c) == 1 and cwd == "C:/p")

    # --- acceptance SALVAGE at exhaustion (HumanEval_56 class: artifact already passes the
    # canonical check, but the turn/transient budget is spent before a clean DONE lands) ----

    # 8. at max_turns but checks ALREADY pass -> salvaged DONE+verified, not MAXTURNS
    w = RelayWorker({"text": "g", "checks": [PASS_CHECK]}, "w6", max_turns=10)
    w.turn, w.status = 10, "ready"        # next turn would exceed the cap
    w._begin_send()
    check("maxturns_salvaged_done", w.status == "done" and w.outcome == "DONE"
          and w.verified is True)

    # 9. at max_turns and checks FAIL -> still MAXTURNS (salvage never masks a real miss)
    w = RelayWorker({"text": "g", "checks": [FAIL_CHECK]}, "w7", max_turns=5)
    w.turn, w.status = 5, "ready"
    w._begin_send()
    check("maxturns_fail_stays_maxturns", w.status == "maxturns" and w.outcome == "MAXTURNS"
          and w.verified is not True)

    # 10. at max_turns with NO checks -> MAXTURNS unchanged (back-compat; nothing to prove)
    w = RelayWorker("plain", "w8", max_turns=3)
    w.turn, w.status = 3, "ready"
    w._begin_send()
    check("maxturns_nocheck_unchanged", w.status == "maxturns" and w.outcome == "MAXTURNS")

    # 11. per-turn timeout, retries exhausted, checks already pass -> salvaged DONE not STUCK
    w = RelayWorker({"text": "g", "checks": [PASS_CHECK]}, "w9",
                    max_transient=0, per_turn_timeout_s=0)
    w.status = "waiting"
    w._t_send = time.time() - 100         # already past the (0s) per-turn timeout
    w._count_before = 0
    term = w.poll()
    check("timeout_salvaged_done", term is True and w.status == "done"
          and w.outcome == "DONE" and w.verified is True)

    # 12. per-turn timeout, retries exhausted, checks FAIL -> STUCK (no false salvage)
    w = RelayWorker({"text": "g", "checks": [FAIL_CHECK]}, "w10",
                    max_transient=0, per_turn_timeout_s=0)
    w.status = "waiting"
    w._t_send = time.time() - 100
    w._count_before = 0
    term = w.poll()
    check("timeout_fail_stays_stuck", term is True and w.status == "stuck"
          and w.outcome == "STUCK")

    print("\n=== %d/%d fleet-verify checks passed ===" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
