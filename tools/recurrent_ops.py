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

Both things this file once deferred are now here. The depth-band -> instruction table came in
item 7 (`_BAND_INSTRUCTIONS`). keep-best/rollback is item A, and the reason it had to wait is
worth keeping, because it did not go away -- it got a boundary:

    "on a verifier that reports pass/fail without a count, no round can be better than another"

That is still true, and it is not a shortcoming to be fixed. `bench/eval_one.py` prints OK or
HIDDEN_TESTS_FAILED and withholds the count ON PURPOSE, so that a solver cannot hill-climb
hidden tests. So keep-best does not ask for a gradient it might not get; it asks whether one
exists, and declines to rank when it does not. A round whose failure count is None is never the
best round, however promising it looks otherwise -- see `_best_round`. Ranking there would be
exactly the benchmark overfitting this project forbids, dressed as an optimisation.

Where a count IS available -- ordinary pytest output, "3 failed" -- a round that lowers it is
real progress, and reverting it because verification has not fully passed yet throws that
progress away. With keep_best on, such a round is KEPT and becomes the baseline the next round
edits; a round that does not lower the count is restored from its own pre-images. Off by
default: it changes what the tree looks like when the loop ends, and that is not a default this
module gets to change silently.
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

#: With keep_best on, a round that lowered the failure count is not reverted -- so when the loop
#: ends the tree carries that round's edits and NOT the state it started in. Callers are told
#: which round they are standing on rather than having to infer it, because "the loop reverts
#: everything" was true before this and is the assumption most likely to be carried forward.
STANDING_ON_START = 0


def depth_band(iteration: int) -> int:
    """Which regime round `iteration` is in: explore broadly, or refine one point."""
    return BAND_EXPLORE if int(iteration) <= SHALLOW_ROUNDS else BAND_REFINE


#: codex-plan item 7 (2026-09-09): the depth-band -> instruction TEXT table this module's own
#: docstring named as deliberately not built here. depth_band() decides WHICH regime a round is
#: in; this decides WHAT the caller should do in it. Kept as a separate table (not folded into
#: depth_band) for the same reason the module already gives: the wording can change without
#: touching the band arithmetic, and a caller that only wants the number never has to parse text.
_BAND_INSTRUCTIONS = {
    BAND_EXPLORE: (
        "EXPLORE (round <= %d): try a genuinely different approach from any prior round, not a "
        "small tweak to the last one -- there is still budget to test more than one hypothesis "
        "before committing to refine any single one. If a prior round already looked promising, "
        "still vary something structural about it (a different mechanism, not just a different "
        "constant or threshold) before narrowing to it." % SHALLOW_ROUNDS
    ),
    BAND_REFINE: (
        "REFINE (round > %d): the explore budget is spent. Read `history` in the state, pick "
        "the most promising direction seen so far, and make ONE targeted, minimal change toward "
        "it. This is not the round to try something unrelated to what has already been tried -- "
        "a new hypothesis here means the explore rounds bought nothing." % SHALLOW_ROUNDS
    ),
}


def depth_instruction(band: int) -> str:
    """The guidance text for depth band `band` (BAND_EXPLORE or BAND_REFINE). Returns "" for an
    unknown band rather than raising -- a caller reading state for a settled run (where
    depth_band is None) must not crash on this lookup."""
    return _BAND_INSTRUCTIONS.get(int(band), "") if band is not None else ""


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


def _countable(rec) -> bool:
    """Whether this round produced a failure COUNT that may be compared with another round's.

    A bool is not a count (isinstance(True, int) is True in Python, and a round recorded as
    fails=True would otherwise rank as one failure). None is not a count either, and that case
    is the important one: `bench/eval_one.py` withholds the number so a solver cannot hill-climb
    hidden tests, so a None here is a verifier keeping a secret on purpose, not a parse failure
    to be worked around.
    """
    f = rec.get("fails")
    return isinstance(f, int) and not isinstance(f, bool)


def _best_round(rounds: list):
    """The round with the fewest failures, or None when no round can be ranked.

    TIES GO TO THE EARLIER ROUND. Two rounds at the same count are not equally good to keep: the
    earlier one is the one whose progress is already banked, and preferring the later one would
    churn the tree for nothing.

    Rounds with no count are not candidates. That is the whole guard -- see the module docstring.
    """
    ranked = [r for r in rounds if _countable(r)]
    if not ranked:
        return None
    return min(ranked, key=lambda r: (int(r["fails"]), int(r.get("iteration") or 0)))


def _standing_on(rounds: list) -> int:
    """Which round's edits the tree currently carries, or STANDING_ON_START for none.

    Read from the log rather than tracked, like everything else here: the caller is a different
    process each round and there is nowhere to keep it.
    """
    standing = STANDING_ON_START
    for r in rounds:
        if r.get("kept"):
            standing = int(r.get("iteration") or standing)
        elif r.get("ok"):
            standing = int(r.get("iteration") or standing)
    return standing


def _pre_images(edits, repo: str):
    """({absolute path: bytes or None}, [paths that could not be read]) for this round's files.

    Its own pre-images, not a repo snapshot. restore_point() cannot serve here: its git mode
    REFUSES a dirty tree, and keep_best deliberately leaves the tree dirty between rounds, while
    its zip mode's roll_back declines to unzip over a live tree on purpose. A round only ever
    needs to undo ITS OWN writes, and undoing exactly that is what leaves an earlier kept round
    intact underneath.

    Bytes, and the same dict shape autoloop._restore consumes, so the undo path is theirs rather
    than a second implementation that can drift from it.

    UNREADABLE FILES ARE RETURNED, NOT ENCODED AS A SENTINEL. A round whose pre-image cannot be
    captured cannot be undone, and edit_and_verify already shows what to do about that: return
    before the first write. The caller refuses the round.
    """
    out = {}
    unreadable = []
    for e in (edits or []):
        try:
            p = autoloop._abs(repo, e["path"])
        except Exception:
            continue
        if p in out or p in unreadable:
            continue
        try:
            out[p] = autoloop._read(p) if autoloop.os.path.exists(p) else None
        except OSError:
            unreadable.append(p)
    return out, unreadable


def _state(run_id: str, cfg: dict, rounds: list, stop: str = CONTINUE, reason: str = "") -> dict:
    best, flat = _progress(rounds)
    nxt = len(rounds) + 1
    band = depth_band(nxt) if stop == CONTINUE else None
    return {
        "run_id": run_id,
        "goal": cfg.get("goal", ""),
        "stop": stop,
        "reason": reason,
        "iterations_done": len(rounds),
        "next_iteration": nxt if stop == CONTINUE else None,
        "max_iter": cfg.get("max_iter"),
        "depth_band": band,
        # what to DO in that band -- read by the caller before it decides this round's edits.
        "instruction": depth_instruction(band) if stop == CONTINUE else None,
        "best_failures": best,
        "rounds_without_improvement": flat,
        "patience": cfg.get("patience"),
        "keep_best": bool(cfg.get("keep_best")),
        # WHICH ROUND THE TREE IS STANDING ON. 0 means the state the loop started in. Reported
        # even when keep_best is off (where it stays 0 until a round passes), because a caller
        # that has to infer this will infer it wrong exactly once.
        "standing_on": _standing_on(rounds),
        "best_iteration": (_best_round(rounds) or {}).get("iteration"),
        # Stated so a caller can tell "no round was better" from "no round could be compared".
        "rankable_rounds": sum(1 for r in rounds if _countable(r)),
        "history": [
            {k: r.get(k) for k in ("iteration", "ok", "stage", "fails", "signal", "reverted",
                                   "kept", "restore_failed")}
            for r in rounds
        ],
    }


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str)


def recurrent_begin(goal: str, verify_command: str = "", repo: str = ".", run_id: str = "",
                    max_iter: int = 5, quality_threshold: int = 0, patience: int = 2,
                    binary_patience: bool = False, keep_best: bool = False) -> str:
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
        keep_best: keep a round whose failure COUNT is lower than every prior round, instead of
            reverting it, and make it the baseline the next round edits. A round with no count
            is never kept -- see the module docstring for why that boundary is deliberate. OFF
            by default: it changes what the tree holds when the loop ends.
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
               "patience": int(patience), "binary_patience": bool(binary_patience),
               "keep_best": bool(keep_best)}
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

    keep_best = bool(cfg.get("keep_best"))
    repo = cfg.get("repo") or "."
    # THE BEST COUNT AS IT STANDS BEFORE THIS ROUND. Read now: after edit_and_verify the log
    # will contain this round too, and "better than every prior round" must not include itself.
    best_before = _best_round(rounds)
    pre = {}
    if keep_best:
        pre, unreadable = _pre_images(edits, repo)
        if unreadable:
            # BEFORE THE FIRST WRITE, as in edit_and_verify. Keeping a round means this function
            # owns the undo, and it will not start a round it could not undo.
            rec = {"kind": KIND_ROUND, "iteration": i, "stop": L.STOPPED,
                   "reason": ("cannot read the pre-image of %d file(s), so this round could not "
                              "be undone if it were not kept: %s"
                              % (len(unreadable), ", ".join(unreadable[:3]))),
                   "ok": False, "stage": "pre-image", "fails": None,
                   "signal": autoloop.SIGNAL_UNKNOWN, "reverted": False, "ts": time.time()}
            runlog_append_local(run_id, rec)
            return _dump(_state(run_id, cfg, rounds + [rec], L.STOPPED, rec["reason"]))

    try:
        # With keep_best the decision to keep cannot be made until the count is in hand, so the
        # edits stay for now and this function reverts them itself when the round did not earn
        # its place. Without it, edit_and_verify's own revert is unchanged.
        result = autoloop.edit_and_verify(edits, verify=cfg.get("verify") or None,
                                          repo=repo, run_id=run_id,
                                          revert_on_fail=not keep_best)
    except Exception as exc:
        if keep_best and pre:
            autoloop._restore(pre)   # an exception mid-round must not leave half a round applied
        return "[recurrent_step error: %s: %s]" % (type(exc).__name__, exc)

    fails = autoloop.count_failures(result.get("output") or "")
    signal = autoloop.failure_signal(result)
    rec = {"kind": KIND_ROUND, "iteration": i, "ok": bool(result.get("ok")),
           "stage": result.get("stage"), "fails": fails, "signal": signal,
           "reverted": bool(result.get("reverted")),
           "binary_patience": bool(cfg.get("binary_patience")), "ts": time.time()}

    if keep_best and not result.get("ok") and not result.get("stopped"):
        # KEEP ONLY ON A LOWER COUNT. Not on a hunch, not on "it looks closer", and never when
        # the count is None -- a verifier that withholds the number is doing so on purpose.
        countable = isinstance(fails, int) and not isinstance(fails, bool)
        improved = countable and (best_before is None or int(fails) < int(best_before["fails"]))
        if improved:
            rec["kept"] = True
            rec["baseline_from"] = int((best_before or {}).get("iteration") or STANDING_ON_START)
        else:
            failed_paths = autoloop._restore(pre)
            rec["kept"] = False
            rec["reverted"] = True
            rec["restore_failed"] = failed_paths
            rec["not_kept_because"] = ("no comparable failure count" if not countable
                                       else "did not lower the failure count")
            if failed_paths:
                # SAY IT IN THE RECORD, not only in a log line: a partially restored round means
                # the tree is in a state no round describes, and the next round must not be
                # generated against a state nobody can name.
                rec["stop"] = L.STOPPED
                rec["reason"] = ("could not restore %d file(s) after an unkept round; the tree "
                                 "is in a state no round describes" % len(failed_paths))
                runlog_append_local(run_id, rec)
                return _dump(_state(run_id, cfg, rounds + [rec], rec["stop"], rec["reason"]))

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
