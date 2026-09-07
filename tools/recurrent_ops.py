# -*- coding: utf-8 -*-
"""The recurrent block, inverted so an agent turn can be the block.

`loop_runner.run` is the same loop written the other way round: it CALLS `candidate(state)`
in-process, once per round. That shape needs a generator that returns synchronously, and the
only caller this repo exposes -- `loop_until_verified` -- satisfies it by taking every round's
edits up front (`edits_per_round`, indexed by iteration). So the exposed path cannot decide
depth at test time: round N's content has to be written before round 1 starts, and when the
list runs out the loop reports `stuck`.

That is the one property the looped-transformer / recurrent-depth mapping actually needs. The
block must produce h_{t+1} FROM h_t, and how many times it runs is decided while running.

So control is inverted here. The agent's turn is the block: `recurrent_step` applies one round,
records what the verification said, evaluates the exit rules, and RETURNS -- the next round's
edits are generated with the previous round's outcome in hand. Depth is a consequence of the
exit rules, not of a list length.

Deliberately reused rather than rebuilt:
  * the exit vocabulary is imported from loop_runner, so the two loops cannot drift apart;
  * the round itself is `autoloop.edit_and_verify`, unchanged -- apply, verify, revert as a set;
  * the state is the runlog. It is already append-only, already keyed by run_id, and already
    where per-iteration records belong, so the loop's history and its audit trail are one thing
    instead of two that can disagree.

Not implemented here, on purpose: the depth-band -> instruction table (this returns the band,
not the template) and any keep-best/rollback rule. Both are being built separately, and a
rollback rule cannot be written yet anyway -- see `failure_signal`: on a verifier that reports
pass/fail without a count, no round can be "better than" another.
"""
from __future__ import annotations

import json
import time

from tools.auto import autoloop
from tools.auto import loop_runner as L
from tools.runlog_ops import _run_path, runlog_append_local
from tools.security import require_unlocked

#: Record kinds this module writes into the runlog. Distinct from autoloop's own "autoloop"
#: records, which edit_and_verify writes for the same round -- one describes the cell, this
#: describes the loop's decision about it.
KIND_BEGIN = "recurrent_begin"
KIND_ROUND = "recurrent_round"

#: Not an exit. The loop says so explicitly rather than returning an empty stop, because "keep
#: going" and "stopped without saying why" must not look alike.
CONTINUE = "continue"

#: Rounds spent broadly before narrowing. The BAND is returned; the instruction text for each
#: band is a separate mapping owned elsewhere, so this file does not need to change when the
#: wording does.
SHALLOW_ROUNDS = 2
BAND_EXPLORE = 1
BAND_REFINE = 2


def depth_band(iteration: int) -> int:
    """Which regime round `iteration` is in: explore broadly, or refine one point."""
    return BAND_EXPLORE if int(iteration) <= SHALLOW_ROUNDS else BAND_REFINE


def _records(run_id: str) -> list:
    try:
        path = _run_path(run_id)
    except ValueError:
        return []
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _config(records: list):
    for r in records:
        if r.get("kind") == KIND_BEGIN:
            return r
    return None


def _rounds(records: list) -> list:
    return [r for r in records if r.get("kind") == KIND_ROUND]


def _progress(rounds: list):
    """(best, flat) over the rounds so far, by the same rule loop_runner uses.

    Recomputed from the log every call rather than carried in a variable, because the caller is
    a separate process each time and there is nowhere to carry it. The log is the state.
    """
    best = None
    flat = 0
    for r in rounds:
        fails = r.get("fails")
        if isinstance(fails, bool) or not isinstance(fails, int):
            fails = None
        if fails is not None:
            if best is None or fails < best:
                best, flat = fails, 0
            else:
                flat += 1
        elif r.get("binary_patience") and r.get("signal") == autoloop.SIGNAL_FAIL:
            flat += 1
    return best, flat


def _state(run_id: str, cfg: dict, rounds: list, stop: str = CONTINUE, reason: str = "") -> dict:
    best, flat = _progress(rounds)
    nxt = len(rounds) + 1
    return {
        "run_id": run_id,
        "goal": cfg.get("goal", ""),
        "stop": stop,
        "reason": reason,
        "iterations_done": len(rounds),
        "next_iteration": nxt if stop == CONTINUE else None,
        "max_iter": cfg.get("max_iter"),
        "depth_band": depth_band(nxt) if stop == CONTINUE else None,
        "best_failures": best,
        "rounds_without_improvement": flat,
        "patience": cfg.get("patience"),
        "history": [
            {k: r.get(k) for k in ("iteration", "ok", "stage", "fails", "signal", "reverted")}
            for r in rounds
        ],
    }


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str)


def recurrent_begin(goal: str, verify_command: str = "", repo: str = ".", run_id: str = "",
                    max_iter: int = 5, quality_threshold: int = 0, patience: int = 2,
                    binary_patience: bool = False) -> str:
    """Open a recurrent loop whose rounds are generated one at a time.

    Unlike `loop_until_verified`, no edits are supplied here. Each round's edits are produced
    after seeing the previous round's result, which is what lets depth be decided while running.

    max_iter has no unlimited setting: the upstream is metered and a runaway loop spends that
    budget on nothing.

    Args:
        goal: what this loop is trying to reach, carried into every state read.
        verify_command: the command whose exit status decides a round. Screened exactly as
            shell_exec screens it.
        repo: working directory for the edits and the verification.
        run_id: identifier; also the runlog key. Reusing a live one is refused rather than
            silently restarted.
        max_iter: hard budget on rounds.
        quality_threshold: stop once the failure count is at or below this.
        patience: rounds without improvement before the loop calls it.
        binary_patience: count a reported-but-uncounted failure as no improvement. Off by
            default for the same reason it is off in loop_runner.
    """
    locked = require_unlocked()
    if locked:
        return locked
    if not isinstance(goal, str) or not goal.strip():
        return "[recurrent_begin: `goal` must be a non-empty string]"
    run_id = (run_id or "").strip() or ("rec_%d" % int(time.time() * 1000))
    if _config(_records(run_id)) is not None:
        return ("[recurrent_begin: run %s already exists. Use recurrent_state to read it, or "
                "choose another run_id -- restarting one in place would leave two loops "
                "interleaved in a single log]" % run_id)
    if verify_command:
        from tools.auto_ops import _screen
        refusal = _screen(verify_command, repo)
        if refusal is not None:
            return refusal
    try:
        max_iter = max(1, int(max_iter))
        cfg = {"kind": KIND_BEGIN, "goal": goal, "verify": verify_command or "", "repo": repo,
               "max_iter": max_iter, "quality_threshold": int(quality_threshold),
               "patience": int(patience), "binary_patience": bool(binary_patience)}
        runlog_append_local(run_id, cfg)
    except Exception as exc:
        return "[recurrent_begin error: %s: %s]" % (type(exc).__name__, exc)
    return _dump(_state(run_id, cfg, []))


def recurrent_state(run_id: str) -> str:
    """What the loop knows so far: the goal, every round's verdict, and whether to keep going.

    Read-only. This is what the next round's edits should be conditioned on.
    """
    records = _records(run_id)
    cfg = _config(records)
    if cfg is None:
        return "[recurrent_state: no such run %s -- call recurrent_begin first]" % run_id
    rounds = _rounds(records)
    settled = [r for r in rounds if r.get("stop") and r.get("stop") != CONTINUE]
    if settled:
        last = settled[-1]
        return _dump(_state(run_id, cfg, rounds, last["stop"], last.get("reason", "")))
    return _dump(_state(run_id, cfg, rounds))


def recurrent_step(run_id: str, edits: list) -> str:
    """Apply one round's edits, verify, record what came back, and say whether to continue.

    EVERY EXIT IS NAMED, and running out of iterations is not success:
        converged / threshold / max_iter / no_progress / stopped / stuck
    Passing an empty `edits` is how a caller says it has nothing further to try; that is `stuck`
    -- an outcome, not an error.
    """
    locked = require_unlocked()
    if locked:
        return locked
    records = _records(run_id)
    cfg = _config(records)
    if cfg is None:
        return "[recurrent_step: no such run %s -- call recurrent_begin first]" % run_id
    rounds = _rounds(records)
    settled = [r for r in rounds if r.get("stop") and r.get("stop") != CONTINUE]
    if settled:
        return ("[recurrent_step: run %s already stopped (%s). A settled loop does not take "
                "another round]" % (run_id, settled[-1]["stop"]))

    i = len(rounds) + 1
    if i > int(cfg["max_iter"]):
        return "[recurrent_step: run %s is at its max_iter budget]" % run_id

    # THE SWITCH IS READ BEFORE THE FIRST WRITE OF THE ROUND, as in loop_runner: stopping after
    # the tree has been edited is not stopping.
    if autoloop.stop_check() != "RUN":
        rec = {"kind": KIND_ROUND, "iteration": i, "stop": L.STOPPED, "reason": "kill switch",
               "ok": False, "stage": "stop", "fails": None,
               "signal": autoloop.SIGNAL_UNKNOWN, "reverted": False, "ts": time.time()}
        runlog_append_local(run_id, rec)
        return _dump(_state(run_id, cfg, rounds + [rec], L.STOPPED, "kill switch"))

    if not edits:
        rec = {"kind": KIND_ROUND, "iteration": i, "stop": L.STUCK,
               "reason": "the caller had nothing further to try", "ok": False, "stage": "stuck",
               "fails": None, "signal": autoloop.SIGNAL_UNKNOWN, "reverted": False,
               "ts": time.time()}
        runlog_append_local(run_id, rec)
        return _dump(_state(run_id, cfg, rounds + [rec], L.STUCK, rec["reason"]))

    try:
        result = autoloop.edit_and_verify(edits, verify=cfg.get("verify") or None,
                                          repo=cfg.get("repo") or ".", run_id=run_id)
    except Exception as exc:
        return "[recurrent_step error: %s: %s]" % (type(exc).__name__, exc)

    fails = autoloop.count_failures(result.get("output") or "")
    signal = autoloop.failure_signal(result)
    rec = {"kind": KIND_ROUND, "iteration": i, "ok": bool(result.get("ok")),
           "stage": result.get("stage"), "fails": fails, "signal": signal,
           "reverted": bool(result.get("reverted")),
           "binary_patience": bool(cfg.get("binary_patience")), "ts": time.time()}

    stop, reason = CONTINUE, ""
    if result.get("stopped"):
        stop, reason = L.STOPPED, "kill switch"
    elif result.get("ok"):
        stop, reason = L.CONVERGED, "verification passed"
    elif isinstance(fails, int) and fails <= int(cfg["quality_threshold"]):
        # STATED, NOT INFERRED -- reaching the threshold is a different outcome from passing,
        # and calling it converged would report a green run that never went green.
        stop, reason = L.THRESHOLD, "failures at or below the threshold"
    else:
        _, flat = _progress(rounds + [rec])
        if flat >= int(cfg["patience"]):
            stop, reason = L.NO_PROGRESS, "no improvement for %d round(s)" % flat
        elif i >= int(cfg["max_iter"]):
            stop, reason = L.MAX_ITER, "budget spent"

    rec["stop"] = stop
    rec["reason"] = reason
    runlog_append_local(run_id, rec)
    return _dump(_state(run_id, cfg, rounds + [rec], stop, reason))
