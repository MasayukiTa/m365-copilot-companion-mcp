# -*- coding: utf-8 -*-
"""Run a cell until it converges, or until a stated condition says stop.

WHAT THIS ADDS TO autoloop.edit_and_verify, WHICH IS ONE ITERATION. That cell applies a change
across files, verifies it, and puts everything back if it does not hold -- once. The loop was
left to the caller, which meant the plan's actual deliverable did not exist: a runner with
`max_iter`, a quality threshold, and an exit condition that is evaluated rather than hoped for.
Reporting the cell as the loop was an over-claim, and this is the part that was missing.

EVERY EXIT IS NAMED. A loop that stops without saying why is indistinguishable from a loop that
crashed, and "it finished" is not a result. The stop reasons are:

    converged        the verification passed
    threshold        the failure count reached the quality threshold
    max_iter         the iteration budget ran out -- NOT a success
    no_progress      the failure count stopped moving for `patience` rounds
    stopped          the kill switch was set between iterations
    stuck            the candidate function could not produce another attempt

THE BUDGET IS NOT OPTIONAL. `max_iter` has no unlimited value, because the risk this module
carries is an unbounded loop against a metered upstream: the tenant quota is 100 generative
messages a minute and a runaway loop spends it on nothing. A caller who wants more must say a
bigger number, in the call, where it is visible.

IT DOES NOT WRITE THE FIX. The caller supplies a `candidate` callable that produces the next
edit set; this module decides whether to keep going and records the trajectory. Keeping the
decision separate from the generation is what lets the loop be tested without a model.
"""
from __future__ import annotations

import time

from tools.auto import autoloop

CONVERGED = "converged"
THRESHOLD = "threshold"
MAX_ITER = "max_iter"
NO_PROGRESS = "no_progress"
STOPPED = "stopped"
STUCK = "stuck"

#: Rounds without the failure count improving before the loop calls it. Two, not one: a single
#: flat round is common when a fix lands in stages, and stopping on it throws away work that was
#: about to converge.
DEFAULT_PATIENCE = 2


def run(candidate, verify=None, repo=".", run_id="", max_iter=5,
        quality_threshold=0, patience=DEFAULT_PATIENCE, custom_state=None,
        timeout_s=autoloop.DEFAULT_TIMEOUT_S):
    """Iterate `candidate` until it converges or a stated condition stops it.

    candidate(state) -> edits, called once per round. `state` carries `iteration`, `history`,
        the last `result`, and whatever the caller put in `custom_state`. Returning a falsy
        value means the caller has nothing further to try, which ends the loop as `stuck` --
        an outcome, not an error.
    verify: the command whose exit status decides, passed through to the cell.
    quality_threshold: stop once the failure count is at or below this. 0 means "no failures".
    max_iter: hard budget. There is deliberately no unlimited setting.

    Returns {stop, iterations, converged, history, last, state}.
    """
    max_iter = max(1, int(max_iter))
    state = {"iteration": 0, "history": [], "result": None,
             "custom": dict(custom_state or {})}
    history = state["history"]
    best = None
    flat = 0
    stop = MAX_ITER

    for i in range(1, max_iter + 1):
        # THE SWITCH IS READ BETWEEN ROUNDS, before anything is generated or written. A loop
        # that only checks at the end is a loop that cannot be stopped.
        if autoloop.stop_check() != "RUN":
            stop = STOPPED
            break

        state["iteration"] = i
        try:
            edits = candidate(state)
        except Exception as exc:
            history.append({"iteration": i, "error": "%s: %s" % (type(exc).__name__, exc)})
            stop = STUCK
            break
        if not edits:
            stop = STUCK
            break

        result = autoloop.edit_and_verify(edits, verify=verify, repo=repo, run_id=run_id,
                                          timeout_s=timeout_s)
        state["result"] = result
        fails = autoloop.count_failures(result.get("output") or "")
        history.append({"iteration": i, "ok": result.get("ok"), "stage": result.get("stage"),
                        "fails": fails, "reverted": result.get("reverted"),
                        "ts": time.time()})

        if result.get("stopped"):
            stop = STOPPED
            break
        if result.get("ok"):
            stop = CONVERGED
            break
        if isinstance(fails, int) and fails <= quality_threshold:
            # STATED, NOT INFERRED. Reaching the threshold is a different outcome from passing,
            # and calling it converged would report a green run that never went green.
            stop = THRESHOLD
            break

        # NO PROGRESS IS AN OUTCOME. An unknown failure count is NOT progress and NOT stagnation
        # -- it is unknown, so it neither resets patience nor spends it. Treating unknown as
        # improvement is how a loop runs its whole budget on a runner it cannot read.
        if isinstance(fails, int):
            if best is None or fails < best:
                best, flat = fails, 0
            else:
                flat += 1
                if flat >= patience:
                    stop = NO_PROGRESS
                    break

    return {
        "stop": stop,
        "iterations": len(history),
        # CONVERGED MEANS THE VERIFICATION PASSED, and nothing else does. Exhausting the budget
        # is not success, and a threshold stop is its own answer.
        "converged": stop == CONVERGED,
        "history": history,
        "last": state.get("result"),
        "state": state,
    }


def describe(outcome, ja=False):
    """One line a person can act on. A stop reason that only a reader of this file understands
    is a stop reason nobody acts on."""
    stop = (outcome or {}).get("stop", "")
    n = (outcome or {}).get("iterations", 0)
    if ja:
        return {
            CONVERGED:   "%d周で収束（検証が通った）" % n,
            THRESHOLD:   "%d周で品質閾値に到達（合格ではない）" % n,
            MAX_ITER:    "%d周で反復上限。収束していない" % n,
            NO_PROGRESS: "%d周で失敗数が動かなくなった。回しても減っていない" % n,
            STOPPED:     "%d周で停止スイッチにより中断" % n,
            STUCK:       "%d周で次の候補が出せなくなった" % n,
        }.get(stop, "%d周で終了 (%s)" % (n, stop))
    return {
        CONVERGED:   "converged after %d iteration(s): the verification passed" % n,
        THRESHOLD:   "reached the quality threshold after %d iteration(s) -- not a pass" % n,
        MAX_ITER:    "ran out of iterations after %d -- did NOT converge" % n,
        NO_PROGRESS: "failures stopped moving after %d iteration(s); iterating is not helping" % n,
        STOPPED:     "halted by the kill switch after %d iteration(s)" % n,
        STUCK:       "no further candidate could be produced after %d iteration(s)" % n,
    }.get(stop, "finished after %d iteration(s) (%s)" % (n, stop))
