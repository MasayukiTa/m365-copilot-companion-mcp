"""Deterministic reliability test for the relay control loop.

Proves the autonomous loop behaves correctly on every terminal path WITHOUT a
browser, by driving run_relay() with a scripted MockDriver:

  DONE            -> success path, notifies completion
  STUCK (no prog) -> identical answers -> stop + notify
  FAIL -> fix     -> recovers and finishes DONE
  STUCK (timeout) -> turn never finishes -> stop + notify
  STUCK (agent)   -> agent reports "STUCK:" -> stop + notify
  MAXTURNS        -> never says DONE -> stop + notify
  ABORTED         -> kill-switch -> stop + notify

Run:  .venv\\Scripts\\python.exe relay\\test_relay_loop.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from relay.copilot_autopilot_relay import run_relay
from tools.gate_ops import stop_clear, stop_request

PY = sys.executable
PASS_CHECK = {"type": "shell", "argv": [PY, "-c", "print('ok')"]}
FAIL_CHECK = {"type": "shell", "argv": [PY, "-c", "import sys;sys.exit(1)"]}


class MockDriver:
    """Scripted driver: returns canned responses; optional per-turn idle flags."""

    def __init__(self, responses, idle_ok=None):
        self.responses = list(responses)
        self.idle_ok = list(idle_ok) if idle_ok is not None else [True] * max(len(responses), 1)
        self.i = -1
        self.sent = []

    def send(self, text):
        self.i += 1
        self.sent.append(text)

    def wait_for_idle(self, timeout_s=0):
        return self.idle_ok[self.i] if self.i < len(self.idle_ok) else True

    def read_last_response(self):
        return self.responses[self.i] if self.i < len(self.responses) else "still working CONTINUE"


def run_case(name, driver, expected, **kw):
    notes = []
    rec = lambda title, body: notes.append((title, body))
    outcome = run_relay(driver, goal=f"test goal: {name}", run_id=f"test_{name}",
                        notify=rec, sleep_s=0, **kw)
    notified = len(notes) == 1
    ok = (outcome == expected) and notified
    flag = "PASS" if ok else "FAIL"
    title = (notes[0][0] if notes else "none").encode("ascii", "replace").decode()
    print(f"[{flag}] {name:<16} outcome={outcome:<9} expected={expected:<9} "
          f"notified={notified} ({title})")
    return ok


def main():
    results = []

    # 1. happy path -> DONE
    results.append(run_case(
        "done_happy",
        MockDriver(["step1 done CONTINUE", "step2 done CONTINUE", "all complete DONE"]),
        "DONE"))

    # 2. no-progress stall -> STUCK
    results.append(run_case(
        "stall_noprog",
        MockDriver(["still working CONTINUE"] * 6),
        "STUCK", max_no_progress=3))

    # 3. FAIL then self-fix then DONE
    results.append(run_case(
        "fail_then_fix",
        MockDriver(["FAIL: missing data.csv", "recreated it CONTINUE", "finished DONE"]),
        "DONE"))

    # 4. turn never finishes (timeout) -> STUCK
    results.append(run_case(
        "timeout_stuck",
        MockDriver([], idle_ok=[False, False]),
        "STUCK", max_timeouts=2))

    # 5. agent self-reports STUCK
    results.append(run_case(
        "agent_stuck",
        MockDriver(["I cannot proceed\nSTUCK: need admin rights"]),
        "STUCK"))

    # 6. never says DONE within the cap -> MAXTURNS
    results.append(run_case(
        "maxturns",
        MockDriver(["a CONTINUE", "b CONTINUE", "c CONTINUE", "d CONTINUE"]),
        "MAXTURNS", max_turns=3))

    # 7. kill-switch -> ABORTED
    stop_request("test kill-switch")
    try:
        results.append(run_case(
            "killswitch",
            MockDriver(["should not run CONTINUE"]),
            "ABORTED"))
    finally:
        stop_clear()  # IMPORTANT: never leave the kill-switch set

    # 8. acceptance gate: DONE + passing check -> DONE (verified, not just claimed)
    results.append(run_case(
        "verify_pass",
        MockDriver(["progress CONTINUE", "all set DONE"]),
        "DONE", checks=PASS_CHECK))

    # 9. acceptance gate: DONE but check keeps failing -> STUCK (VERIFY_FAILED) at the cap
    results.append(run_case(
        "verify_fail_cap",
        MockDriver(["claim DONE", "still claim DONE", "still claim DONE"]),
        "STUCK", checks=FAIL_CHECK, max_verify_attempts=2))

    total = len(results)
    passed = sum(results)
    print(f"\n=== {passed}/{total} scenarios passed ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
