"""Tests for transient-failure retries in the fleet worker (Claude-Code-style retry of
network/tool/send hiccups before giving up). No browser; drives the worker directly with
a fake driver, skipping the backoff cooldown.

Run:  .venv\\Scripts\\python.exe relay\\test_transient.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from relay.relay_fleet import RelayWorker

results = []


def check(name, cond):
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


class _Loc:
    def count(self):
        return 0


class FailSendDriver:
    """send() raises `fails` times, then succeeds."""
    def __init__(self, fails):
        self.fails = fails
        self.calls = 0
        self._count_before = 0

    def _answers(self):
        return _Loc()

    def send(self, text):
        self.calls += 1
        if self.calls <= self.fails:
            raise RuntimeError("send boom %d" % self.calls)

    def read_last_response(self):
        return ""


def main():
    # 1. send failures retry (don't consume a turn) then succeed
    w = RelayWorker("g", "w0", max_transient=3)
    w.drv = FailSendDriver(fails=2)
    w.status = "ready"
    w._begin_send()
    check("send_fail_retry1", w.status == "ready" and w.transient == 1 and w.turn == 0)
    w._cooldown_until = 0
    w._begin_send()
    check("send_fail_retry2", w.status == "ready" and w.transient == 2 and w.turn == 0)
    w._cooldown_until = 0
    w._begin_send()
    check("send_succeeds_after_retries", w.status == "waiting" and w.turn == 1)

    # 2. send failures exhaust the budget -> STUCK
    w = RelayWorker("g", "w1", max_transient=2)
    w.drv = FailSendDriver(fails=99)
    w.status = "ready"
    for _ in range(5):
        w._cooldown_until = 0
        if w.status in ("stuck",):
            break
        w._begin_send()
    check("send_budget_exhausted_stuck", w.status == "stuck" and w.outcome == "STUCK"
          and "after 2 retries" in (w.reason or ""))

    # 3. agent STUCK is retried with the RETRY nudge, then terminal at the budget
    w = RelayWorker("g", "w2", max_transient=2)
    w.last_response = ""
    w._decide("STUCK: tool failed")
    check("agent_stuck_retry1", w.status == "ready" and w.transient == 1
          and "一時的な失敗" in w.job)
    w._decide("STUCK: still")
    check("agent_stuck_retry2", w.status == "ready" and w.transient == 2)
    w._decide("STUCK: still")
    check("agent_stuck_terminal", w.status == "stuck" and w.outcome == "STUCK"
          and "after 2 retries" in (w.reason or ""))

    # 4. a real (non-stuck) response resets the transient budget
    w = RelayWorker("g", "w3", max_transient=5)
    w._decide("STUCK: x")
    check("reset_pre", w.transient == 1)
    w._decide("making progress CONTINUE")
    check("reset_on_progress", w.transient == 0 and w.status == "ready")

    # 5. timeout in the waiting state retries, then STUCK at the budget
    import time
    w = RelayWorker("g", "w4", max_transient=1, per_turn_timeout_s=0)
    w.drv = FailSendDriver(fails=0)
    w.status = "waiting"
    w._t_send = time.time() - 100   # already past the (0s) timeout
    w._count_before = 0
    term = w.poll()
    check("timeout_retry", w.status == "ready" and w.transient == 1 and term is False)
    w._cooldown_until = 0
    w.status = "waiting"; w._t_send = time.time() - 100
    term = w.poll()
    check("timeout_terminal", w.status == "stuck" and w.outcome == "STUCK")

    print("\n=== %d/%d transient-retry checks passed ===" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
