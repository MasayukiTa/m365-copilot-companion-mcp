"""Tests for the FLEET refuter (operator B in the worker) and attach robustness.

No browser: the non-blocking RefuterSession is replaced with a scripted fake, and the
attach-recovery path is driven with fake contexts. Proves:
  * candidate DONE -> refuter UPHELD -> done
  * candidate DONE -> refuter REFUTED -> reason fed back, back to 'ready', counted
  * refute budget honoured (no more reviews past max_refute -> DONE stands)
  * checks + refuter compose (verify pass -> refuting -> done verified)
  * a refuter that needs a couple of polls (non-blocking) still settles
  * attach failing while the WHOLE Edge died -> FleetContextLost (goal resumable);
    a one-off open failure on a live context -> worker ERROR, run completes

Run:  .venv\\Scripts\\python.exe relay\\test_fleet_refute.py
"""
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import relay.refuter as refuter_mod
from relay.copilot_autopilot_relay import REFUTE_FIX_JOB
from relay.relay_fleet import FleetContextLost, RelayWorker, TERMINAL, run_relay_fleet

PY = sys.executable
PASS_CHECK = {"type": "shell", "argv": [PY, "-c", "print('ok')"]}
results = []


def check(name, cond):
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


class FakeSession:
    """Scripted stand-in for RefuterSession. Each instance pops the next verdict; if
    `polls_until` > 0 it returns None that many times first (to exercise non-blocking)."""
    scripted = []
    polls_until = 0

    def __init__(self, context, base_url, goal, final, **kw):
        self._v = FakeSession.scripted.pop(0) if FakeSession.scripted else ("UPHELD", "")
        self._left = FakeSession.polls_until
        self.lens = kw.get("lens", "")

    def start(self):
        return self

    def poll(self):
        if self._left > 0:
            self._left -= 1
            return None
        return self._v

    def close(self):
        pass


def settle(w, steps=400):
    for _ in range(steps):
        if w.status not in ("verifying", "refuting"):
            return
        w.poll()
        time.sleep(0.005)


def worker(refuter=True, max_refute=2, checks=None, review_lenses=None):
    g = {"text": "g", "checks": checks} if checks else "g"
    w = RelayWorker(g, "w0", refuter=refuter, max_refute=max_refute,
                    review_lenses=review_lenses)
    w._context = object()          # truthy -> refuter allowed
    w._agent_url = "https://m365.cloud.microsoft/chat/agent/T_x.y"
    return w


def main():
    refuter_mod.RefuterSession = FakeSession   # patch the class the worker imports

    # 1. candidate DONE -> UPHELD -> done
    FakeSession.scripted = [("UPHELD", "")]; FakeSession.polls_until = 0
    w = worker()
    w._decide("all set DONE")
    check("upheld_enters_refuting", w.status == "refuting" and w.refute_count == 1)
    settle(w)
    check("upheld_done", w.status == "done" and w.outcome == "DONE")

    # 2. REFUTED -> reason fed back, back to ready, counted
    FakeSession.scripted = [("REFUTED", "境界値が未対応")]
    w = worker()
    w._decide("DONE")
    settle(w)
    check("refuted_ready", w.status == "ready" and w.refute_count == 1)
    check("refuted_feedback", "境界値が未対応" in w.job
          and REFUTE_FIX_JOB.split("%s")[0][:20] in w.job)

    # 3. budget: max_refute=1 -> after one review, a re-claimed DONE is accepted
    FakeSession.scripted = [("REFUTED", "a"), ("REFUTED", "b")]
    w = worker(max_refute=1)
    w._decide("DONE")          # review 1 -> refuted -> ready
    settle(w)
    w._on_done_claimed()       # agent re-claims DONE; budget exhausted -> accept
    settle(w)
    check("budget_capped_done", w.status == "done" and w.refute_count == 1)

    # 4. checks + refuter compose: verify pass -> refuting -> upheld -> done verified
    FakeSession.scripted = [("UPHELD", "")]
    w = worker(checks=[PASS_CHECK])
    w._decide("DONE")
    settle(w)
    check("checks_then_refute_done", w.status == "done" and w.verified is True)

    # 5. non-blocking: a refuter that needs a few polls still settles to done
    FakeSession.scripted = [("UPHELD", "")]; FakeSession.polls_until = 3
    w = worker()
    w._decide("DONE")
    settle(w)
    check("nonblocking_settles", w.status == "done")
    FakeSession.polls_until = 0

    # 6. refuter OFF -> candidate done is accepted immediately (back-compat)
    w = worker(refuter=False)
    w._decide("DONE")
    check("refuter_off_immediate_done", w.status == "done" and w.outcome == "DONE")

    # 6b. FLEET PANEL: three lenses run in turn; majority REFUTED -> reinject combined
    FakeSession.scripted = [("REFUTED", "境界値"), ("REFUTED", "型不一致"), ("UPHELD", "")]
    w = worker(review_lenses=["correctness", "edge", "security"])
    w._decide("DONE")
    settle(w)
    check("fleet_panel_majority_reinject", w.status == "ready"
          and "境界値" in w.job and "型不一致" in w.job)

    # 6c. FLEET PANEL: minority refute -> upheld -> done
    FakeSession.scripted = [("REFUTED", "x"), ("UPHELD", ""), ("UPHELD", "")]
    w = worker(review_lenses=["correctness", "edge", "security"])
    w._decide("DONE")
    settle(w)
    check("fleet_panel_minority_done", w.status == "done" and w.outcome == "DONE")

    # --- attach robustness ---
    class FlakyContext:
        """cookies() works once (loop-top probe), but the Edge 'dies' the moment we open a
        tab -> new_page raises and cookies() then fails = whole context lost."""
        def __init__(self): self.dead = False
        def cookies(self):
            if self.dead:
                raise RuntimeError("context closed")
            return []
        def new_page(self):
            self.dead = True
            raise RuntimeError("Target page, context or browser has been closed")

    class OneOffFailContext:
        """A single open fails but the context is alive (cookies ok) -> not a context loss."""
        def cookies(self): return []
        def new_page(self): raise RuntimeError("boom one-off")

    raised = False
    try:
        run_relay_fleet(FlakyContext(), ["goalA"], "u", poll_s=0, max_concurrent=1,
                        notify=lambda *a: None)
    except FleetContextLost as e:
        raised = True
        unfinished_ok = any((g.get("text") if isinstance(g, dict) else g) == "goalA"
                            for g in e.unfinished)
    check("dead_context_raises_lost", raised and unfinished_ok)

    res = run_relay_fleet(OneOffFailContext(), ["goalB"], "u", poll_s=0, max_concurrent=1,
                          notify=lambda *a: None)
    check("livectx_oneoff_error_completes",
          len(res) == 1 and res[0]["outcome"] == "ERROR")

    print("\n=== %d/%d fleet-refute checks passed ===" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
