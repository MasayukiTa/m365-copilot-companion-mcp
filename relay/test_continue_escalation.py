"""Tests for the CONTINUE-nudge escalation/cap fix (relay_fleet.RelayWorker + refuter.py).

CONFIRMED ROOT CAUSE (from live transcripts): a worker whose agent keeps responding but
never emits DONE got the IDENTICAL continue nudge re-injected into the SAME conversation
every turn, up to max_turns -- degrading the M365 Copilot model until it refused to answer.
The `no_progress` guard only catches a VERBATIM-identical agent reply; a task that produces
slightly-different prose each turn (never converging, never DONE) rode the plain CONTINUE
branch all the way to max_turns while WE re-sent byte-identical nudge text every turn. The
refuter's own UNCLEAR-verdict retry loop had the same disease on a smaller scale.

No browser: drives RelayWorker._decide() directly with scripted (non-identical) responses,
and calls the pure nudge-selector functions directly. Proves:

  * _continue_nudge(count) escalates -- counts 1-2 are the original CONTINUE_JOB (back-compat),
    counts 3+ differ from each other and from CONTINUE_JOB, and consecutive counts are never
    byte-identical.
  * Feeding N distinct non-DONE responses to a worker does NOT produce N identical
    self.job values once past the gentle window.
  * After max_continue non-DONE turns, the worker TERMINATES (STUCK) instead of issuing
    another CONTINUE job -- the hard cap, independent of the verbatim no_progress guard.
  * A DONE response still completes normally (no regression), and resets the continue streak.
  * A steer still preempts CONTINUE and resets the continue streak (no regression).
  * The verbatim-identical no_progress path still fires STUCK on its own budget, separately
    from (and before) the continue-count cap.
  * refuter._next_refuter_nudge(count) rotates through varied text -- never repeats the same
    text on consecutive counts, and never raises for any count (including past the cap).

Run:  .venv\\Scripts\\python.exe relay\\test_continue_escalation.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from relay.relay_fleet import RelayWorker, TERMINAL, _continue_nudge
from relay.copilot_autopilot_relay import CONTINUE_JOB
from relay.refuter import _next_refuter_nudge, REFUTER_NUDGE

results = []


def check(name, cond):
    results.append(bool(cond))
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


def main():
    # --- 1. pure _continue_nudge escalation ladder ---
    check("nudge_1_is_continue_job", _continue_nudge(1) == CONTINUE_JOB)
    check("nudge_2_is_continue_job", _continue_nudge(2) == CONTINUE_JOB)
    n3, n4, n5, n6 = (_continue_nudge(c) for c in (3, 4, 5, 6))
    check("nudge_3_differs_from_continue_job", n3 != CONTINUE_JOB)
    check("nudge_escalation_no_two_consecutive_identical",
          len({n3, n4}) == 2 and len({n4, n5}) == 2 and len({n5, n6}) == 2)
    check("nudge_embeds_count", "3" in n3 and "4" in n4)
    # never raises for any count, including well past any realistic cap
    try:
        for c in range(1, 50):
            _continue_nudge(c)
        check("nudge_never_raises", True)
    except Exception:
        check("nudge_never_raises", False)

    # --- 2. worker fed distinct non-DONE responses -> jobs are not all identical ---
    w = RelayWorker("some goal", "w0", max_continue=8, max_no_progress=100)
    jobs = []
    for i in range(6):
        w._decide("still working on step %d, making progress CONTINUE" % i)
        jobs.append(w.job)
    check("worker_alive_through_escalation_window", w.status == "ready")
    check("jobs_not_all_identical", len(set(jobs)) > 1)
    check("consecutive_jobs_in_escalation_zone_differ", jobs[-1] != jobs[-2])

    # --- 3. hard cap: after max_continue non-DONE turns, terminate instead of CONTINUE ---
    w = RelayWorker("some goal", "w1", max_continue=4, max_no_progress=100)
    for i in range(3):
        w._decide("progress note %d, still going CONTINUE" % i)
        check("cap_not_yet_tripped_%d" % i, w.status == "ready")
    w._decide("progress note 3, still going CONTINUE")
    check("cap_trips_to_terminal", w.status in TERMINAL)
    check("cap_outcome_is_stuck", w.outcome == "STUCK")
    check("cap_reason_mentions_continue_nudges", "continue nudge" in (w.reason or "").lower())
    # once terminal, poll() itself must short-circuit (never call _decide again for a
    # terminal worker) -- this is the real invariant that stops the round-robin sweep from
    # sending it any further turn, nudge or otherwise.
    check("poll_short_circuits_once_terminal", w.poll() is True and w.status in TERMINAL)

    # --- 3b. the hard cap salvages via checks if they already pass (reuses existing gate) ---
    PY = sys.executable
    PASS_CHECK = {"type": "shell", "argv": [PY, "-c", "print('ok')"]}
    w = RelayWorker({"text": "g", "checks": [PASS_CHECK]}, "w1b", max_continue=2,
                     max_no_progress=100)
    w._decide("progress a, still working CONTINUE")
    check("salvage_pre_cap_ready", w.status == "ready")
    w._decide("progress b, still working CONTINUE")
    check("salvage_cap_trip_is_done_not_stuck", w.status == "done" and w.outcome == "DONE"
          and w.verified is True)

    # --- 4. DONE still completes normally (no regression) ---
    w = RelayWorker("some goal", "w2", max_continue=3)
    w._decide("progress note, still going CONTINUE")
    w._decide("progress note 2, still going CONTINUE")
    check("continue_count_advanced", w._continue_count == 2)
    w._decide("all done now DONE")
    check("done_still_completes", w.status == "done" and w.outcome == "DONE")
    check("done_resets_continue_count", w._continue_count == 0)

    # --- 5. a steer still preempts CONTINUE and resets the streak ---
    w = RelayWorker("some goal", "w3", max_continue=3)
    w._decide("progress note, still going CONTINUE")
    w._decide("progress note 2, still going CONTINUE")
    check("continue_count_advanced_pre_steer", w._continue_count == 2)
    w.steer("追加指示: ログも出力して")
    w._begin_send()   # consumes the queued steer, sets _last_was_steer
    check("steer_consumed", w._last_was_steer is True)
    w._decide("added logging as requested CONTINUE")
    check("steer_bridges_off_steer_not_raw_continue",
          "先ほどの追加指示" in w.job and w.job != CONTINUE_JOB)
    check("steer_resets_continue_count", w._continue_count == 0)

    # --- 6. the verbatim-identical no_progress guard still works, independently ---
    # (>=160 chars so this doesn't trip the SEPARATE short-response card-stall/INFRA_STUCK
    # branch, which is an unrelated pre-existing guard also gated on no_progress -- keep
    # this test isolated to the plain "no progress" STUCK path.)
    stalled_reply = ("identical stalled reply that never changes CONTINUE " + "x" * 160)
    w = RelayWorker("some goal", "w4", max_no_progress=3, max_continue=100)
    for _ in range(4):
        w._decide(stalled_reply)
    check("no_progress_guard_still_fires_stuck", w.status == "stuck" and w.outcome == "STUCK")
    check("no_progress_reason_unchanged", "no progress" in (w.reason or ""))

    # --- 7. refuter nudge: pure, varies, never raises ---
    r1, r2, r3 = (_next_refuter_nudge(c) for c in (1, 2, 3))
    check("refuter_nudge_1_is_original", r1 == REFUTER_NUDGE)
    check("refuter_nudge_varies", r1 != r2)
    check("refuter_nudge_rotates_safely", r3 == r1)  # 2 variants -> wraps back at count 3
    try:
        for c in range(1, 20):
            _next_refuter_nudge(c)
        check("refuter_nudge_never_raises", True)
    except Exception:
        check("refuter_nudge_never_raises", False)

    print("\n=== %d/%d continue-escalation checks passed ===" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
