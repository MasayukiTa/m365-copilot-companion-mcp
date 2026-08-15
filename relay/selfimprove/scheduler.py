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
                  baseline_path=None) -> list:
    """Every reason this run should not start. Empty means it may.

    Returns reasons rather than raising so a caller can log all of them at once: fixing one
    and rediscovering the next on the following night is how a scheduled loop spends a week
    not running.
    """
    reasons = []

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

    if activate and not operator_approved_activation:
        reasons.append("activation is on but no operator approved it for this schedule; a "
                       "scheduled run that installs its own winner changes the system while "
                       "nobody is watching")

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
    path = lock_path or DEFAULT_LOCK
    if lock_held(path):
        raise Blocked("a campaign is already in flight")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
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
