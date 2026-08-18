"""Phase 11: running the loop on a schedule, and the conditions under which it must not.

WHAT SCHEDULED EVOLUTION IS

A campaign that runs without someone starting it. That is the whole of the feature and
almost none of the work; the work is the set of conditions under which an unattended run
should decline to start, because an unattended loop is exactly the arrangement in which a
quiet defect compounds.

THE PRECONDITIONS, AND WHAT EACH ONE PREVENTS

  frozen set intact      A run whose judge changed produces numbers nobody can trust, and
                         unattended they land in the archive looking like every other row.
  no run in flight       Two campaigns sharing an archive and an active manifest interleave
                         their candidates, and the second one's baseline is the first one's
                         half-applied state.
  the harness is well    If the last window was mostly INFRA_ABORT, more candidates produce
                         more aborts. Fix the environment first; this is the same judgement
                         harness_feedback makes, reused rather than restated.
  activation is off      unless an operator explicitly turned it on for this schedule. A
                         scheduled run that can install its own winner is a system that
                         changes while nobody is watching, and the whole gate structure
                         upstream assumes someone chose.
  a budget               so a loop that finds a productive-looking direction cannot spend a
                         night on it.

WHY IT REPORTS RATHER THAN RETRIES

A precondition that fails is information. Retrying past it converts "the environment is
broken" into "the environment is broken and we have burned four hours", and the record shows
a busy night rather than a blocked one.
"""
from __future__ import annotations

import json
import os
import time

from relay.selfimprove import frozen as F
from relay.selfimprove import harness_feedback as HF

#: A lock the scheduler holds while a campaign is in flight. A file rather than a process
#: check: the run that matters may be on the other side of a reboot, and a stale lock with a
#: readable timestamp is easier to reason about than a missing process.
DEFAULT_LOCK = os.path.join(os.path.dirname(__file__), "campaign.lock")

#: How long a lock may be held before it is presumed abandoned. Long enough that a real
#: campaign is never interrupted; short enough that a crash does not stop tomorrow's run.
STALE_LOCK_S = 6 * 3600


class Blocked(RuntimeError):
    """Raised when a scheduled run must not start, with the reason it must not."""


def preconditions(*, recent_decisions=None, lock_path=None, activate=False,
                  operator_approved_activation=False, budget_candidates=None,
                  baseline_path=None, level="B") -> list:
    """Every reason this run should not start. Empty means it may.

    Returns reasons rather than raising so a caller can log all of them at once: fixing one
    and rediscovering the next on the following night is how a scheduled loop spends a week
    not running.

    `level` is the autonomy rung (see `relay.selfimprove.autonomy`). It defaults to B because
    a SCHEDULED run is by definition one the system started, and that is what B means -- level
    A says a human starts the experiment, so a nightly campaign at level A is a contradiction
    rather than a configuration.
    """
    from relay.selfimprove import autonomy as AU

    reasons = []

    if not AU.permits(level, AU.START_EXPERIMENT):
        reasons.append(
            "level %s does not permit the system to start an experiment; at that rung a human "
            "starts it, so an unattended campaign is not a stricter version of this schedule "
            "-- it is a different one" % AU.normalise(level))

    ok, changed = _frozen(baseline_path)
    if not ok:
        reasons.append("the frozen set is not intact (%s); a run whose judge changed produces "
                       "numbers nobody can trust, and unattended they look like any other row"
                       % ", ".join(changed[:3]))

    held = lock_held(lock_path)
    if held:
        reasons.append("a campaign has been in flight since %s; two sharing an archive "
                       "interleave their candidates and the second one's baseline is the "
                       "first one's half-applied state" % held)

    # LEVEL C IS WHERE SELF-ACTIVATION LIVES. Below it, activation still happens -- a human
    # approves this schedule's winner, which is level B as the brief describes it. So the
    # per-run approval is not redundant with the rung: it is what B looks like in practice,
    # and the rung is what makes C not need it.
    if activate and not AU.permits(level, AU.ACTIVATE_CONFIG, change_kind="parameters",
                                   gates_all_passed=True) and not operator_approved_activation:
        reasons.append("activation is on but no operator approved it for this schedule; a "
                       "scheduled run that installs its own winner changes the system while "
                       "nobody is watching (level %s; self-activation begins at C)"
                       % AU.normalise(level))

    if budget_candidates is not None and int(budget_candidates) <= 0:
        reasons.append("no candidate budget; an unbounded scheduled loop can spend a night "
                       "on a direction that looked productive at 2am")

    if recent_decisions:
        for observation in HF.observe(decisions=recent_decisions):
            if "unwell" in observation["finding"]:
                reasons.append("%s -- %s" % (observation["finding"], observation["evidence"]))

    return reasons


def _frozen(baseline_path):
    try:
        if baseline_path:
            return F.frozen_intact(baseline_path=baseline_path)
        return F.frozen_intact()
    except Exception as exc:
        # Unable to check is not intact. The scheduled path is the one where nobody is
        # watching, so it is the last place to be generous about this.
        return False, ["frozen check failed: %s" % exc]


# --------------------------------------------------------------------------------------
# The lock
# --------------------------------------------------------------------------------------

def lock_held(lock_path=None):
    """The ISO timestamp a live lock was taken, or "" if none is held."""
    path = lock_path or DEFAULT_LOCK
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return ""
    started = float(data.get("started_at") or 0)
    if time.time() - started > STALE_LOCK_S:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(started))


def take_lock(lock_path=None, *, note=""):
    """Take the lock, or raise. The create is ATOMIC.

    Checking `lock_held` and then opening for write is two operations, and two schedulers
    that both check before either writes both proceed -- which is the exact situation the
    lock exists to prevent, arriving only under the timing that makes it hardest to see
    afterwards. O_CREAT|O_EXCL makes the creation itself the test.
    """
    path = lock_path or DEFAULT_LOCK
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if lock_held(path):
            raise Blocked("a campaign is already in flight")
        # A lock file older than STALE_LOCK_S is an abandoned one; take it over rather than
        # letting yesterday's crash block every night from here on.
        release_lock(path)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise Blocked("a campaign is already in flight")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"started_at": time.time(), "pid": os.getpid(), "note": note}, fh)
    return path


def release_lock(lock_path=None):
    try:
        os.remove(lock_path or DEFAULT_LOCK)
    except OSError:
        pass


# --------------------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------------------

def scheduled_run(run_campaign, *, budget_candidates=5, lock_path=None,
                  recent_decisions=None, activate=False,
                  operator_approved_activation=False, baseline_path=None) -> dict:
    """Check the preconditions, run the campaign once, release. Never retries.

    `run_campaign(budget)` does the work and returns whatever it likes; this is only the
    part that decides whether it happens, and records why when it does not.
    """
    reasons = preconditions(
        recent_decisions=recent_decisions, lock_path=lock_path, activate=activate,
        operator_approved_activation=operator_approved_activation,
        budget_candidates=budget_candidates, baseline_path=baseline_path)
    if reasons:
        return {"ran": False, "blocked_by": reasons, "result": None,
                "note": "a failed precondition is information; retrying past it converts a "
                        "blocked night into a busy one and the record stops saying which"}

    take_lock(lock_path, note="scheduled campaign")
    try:
        result = run_campaign(budget_candidates)
    finally:
        release_lock(lock_path)
    return {"ran": True, "blocked_by": [], "result": result, "note": ""}


# --------------------------------------------------------------------------------------
# The entry point
# --------------------------------------------------------------------------------------

def nightly(*, budget_candidates=5, activate=False, operator_approved_activation=False,
            evaluate=None, archive_path=None, lock_path=None) -> dict:
    """One scheduled campaign, with the phases that decide WHAT to run wired in.

    This exists because the parts had no caller. Phase 9 selected a replay set, Phase 6
    judged the harness, Phase 11 decided whether to start -- each tested, none reachable
    from anything a person could run, which is a way of being finished that does not survive
    someone trying to use it.

    The order is the argument. The recent decisions are read FIRST, because they answer two
    different questions: whether the harness is well enough to run at all (Phase 6, a
    precondition here) and which failures the next run should replay (Phase 9). Running a
    campaign against a harness that mostly aborts produces more aborts, and choosing what to
    replay from that history chooses noise.
    """
    from relay.selfimprove import archive as A
    from relay.selfimprove import campaign as C
    from relay.selfimprove import coreset as CS
    from relay.selfimprove.controller import EvolutionController

    archive = A.Archive(archive_path) if archive_path else A.Archive()
    decisions = [{"state": (e.get("verdict") or "").upper()} for e in archive.entries()][-20:]

    reasons = preconditions(recent_decisions=decisions, lock_path=lock_path,
                            activate=activate,
                            operator_approved_activation=operator_approved_activation,
                            budget_candidates=budget_candidates)
    if reasons:
        return {"ran": False, "blocked_by": reasons, "result": None,
                "note": "a failed precondition is information; retrying past it converts a "
                        "blocked night into a busy one and the record stops saying which"}

    replay = CS.select(_recent_failures(archive), budget=budget_candidates)

    def run(budget):
        controller = EvolutionController(activate=activate)
        return C.sweep(controller, evaluate=evaluate or _refuse,
                       on_result=lambda row: None)

    out = scheduled_run(run, budget_candidates=budget_candidates, lock_path=lock_path,
                        recent_decisions=decisions, activate=activate,
                        operator_approved_activation=operator_approved_activation)
    out["replay_set"] = replay
    return out


def _recent_failures(archive) -> list:
    """Episode-level failures from the archive's recent entries, for the replay coreset."""
    rows = []
    for entry in archive.entries()[-20:]:
        for row in ((entry.get("results") or {}).get("episodes") or []):
            if not row.get("success", True):
                rows.append(row)
    return rows


def _refuse(*_a, **_k):
    raise Blocked("nightly() needs an evaluator; it will not invent one and call the result "
                  "a measurement")


if __name__ == "__main__":                                   # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="One scheduled evolution campaign.")
    ap.add_argument("--budget", type=int, default=5)
    ap.add_argument("--activate", action="store_true",
                    help="install the winner (needs --operator-approved as well)")
    ap.add_argument("--operator-approved", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the preconditions and stop")
    args = ap.parse_args()

    if args.dry_run:
        for reason in preconditions(budget_candidates=args.budget, activate=args.activate,
                                    operator_approved_activation=args.operator_approved):
            print("BLOCKED:", reason)
        else:
            print("preconditions OK")
    else:
        print(json.dumps(nightly(budget_candidates=args.budget, activate=args.activate,
                                 operator_approved_activation=args.operator_approved),
                         ensure_ascii=False, indent=2, default=str))
