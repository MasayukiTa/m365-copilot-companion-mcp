"""Phase 11: the autonomy ladder as a thing that can be checked, not a pair of booleans.

WHAT WAS ALREADY HERE, AND WHAT WAS NOT

`scheduler` runs the loop unattended and refuses to start under conditions that would make
an unattended run untrustworthy. That is most of scheduled evolution. What it did not have is
the LADDER: the brief names four levels, and the system carried two booleans -- `activate`
and `operator_approved_activation` -- which encode four states with no names. Four unnamed
states cannot be reasoned about, cannot be reported, and cannot refuse a transition, because
there is nothing there to transition between.

THE LEVELS, AS THE BRIEF STATES THEM

  A   a human starts the experiment and a human approves the winner
  B   the system proposes and evaluates on its own; a human approves activation
  C   the system activates typed/config-only changes that passed every gate
  D   the system opens source-code pull requests; a human reviews them

THE TWO THINGS NO LEVEL PERMITS

Activating a source-code change without review, and merging. The brief says not to jump to
unrestricted autonomous source-code mutation, and the way that jump actually happens is not
a decision to make it -- it is level D plus one convenience function that merges its own PR
when the checks are green. So those two actions are absent from every level rather than
being reachable at a high one, and `permits` returns False for them at D exactly as at A.

WHY THE LOOP MAY NOT SET ITS OWN LEVEL

Every guard in the preceding phases is enforced somewhere the loop can reach. If the level
that decides which guards apply is itself a value the loop can change, all of them become
advisory in one step. So `autonomy` joins `provenance` and `frozen` among the components a
genome may not touch, and a raise requires an operator and cannot skip a rung: A to C would
grant self-activation without the intervening period where the system proposed, a human
approved each winner, and someone could see whether its proposals were any good. That period
is the evidence for the raise. Skipping it is skipping the evidence.

LOWERING IS NOT SYMMETRIC

Dropping to a stricter level needs no approval and is always allowed. A tripwire firing at
2am should be able to put the system back to B without waiting for someone to wake up, and a
guard that can only be tightened by the person who is not there is a guard that stays loose.
"""
from __future__ import annotations

#: Rungs, weakest first. Position is meaningful -- `raise_to` refuses to skip one.
LEVELS = ("A", "B", "C", "D")

#: What the system may do without a human in the moment.
START_EXPERIMENT = "start_experiment"
EVALUATE = "evaluate"
ACTIVATE_CONFIG = "activate_config"
OPEN_SOURCE_PR = "open_source_pr"

#: Actions no level grants. Listed rather than omitted so that "is this reachable at D?" has
#: an answer in the code instead of in someone's memory of the design.
ACTIVATE_SOURCE = "activate_source"
MERGE_SOURCE_PR = "merge_source_pr"
NEVER_PERMITTED = {
    ACTIVATE_SOURCE: ("no level activates a source-code change without review; the ladder's "
                      "top rung is opening a pull request, and the human reading it is the "
                      "point of the rung"),
    MERGE_SOURCE_PR: ("no level merges; a loop that opens a PR and merges it when the checks "
                      "go green is unrestricted source-code mutation with an audit trail, "
                      "which is the thing the brief says not to jump to"),
}

#: Which actions each rung adds. Cumulative -- a level permits its own row and every row
#: above it in the tuple.
_GRANTS = {
    "A": (EVALUATE,),
    "B": (START_EXPERIMENT,),
    "C": (ACTIVATE_CONFIG,),
    "D": (OPEN_SOURCE_PR,),
}

#: What `ACTIVATE_CONFIG` is allowed to be. The brief says typed/config-only, and the reason
#: is that a typed parameter change is reversible by writing the old value back, which is
#: what makes unattended activation recoverable at all.
CONFIG_KINDS = frozenset({"parameters", "components", "config"})


class AutonomyError(ValueError):
    """Raised when an action, or a change of level, is not permitted."""


def normalise(level) -> str:
    value = str(level or "").strip().upper()
    if value not in LEVELS:
        # UNKNOWN MEANS THE FLOOR, not an error and not the top. A misspelled level in a
        # config file should cost a night of autonomy, never grant one.
        return LEVELS[0]
    return value


def granted(level) -> set:
    """Every action this level permits, cumulatively."""
    level = normalise(level)
    stop = LEVELS.index(level)
    out = set()
    for rung in LEVELS[:stop + 1]:
        out.update(_GRANTS[rung])
    return out


def permits(level, action, *, change_kind=None, gates_all_passed=None) -> bool:
    """Whether this level permits this action, in these circumstances.

    `change_kind` and `gates_all_passed` matter only for `ACTIVATE_CONFIG`, and they are
    checked here rather than by the caller because an activation path that reads the level and
    then decides for itself whether the change was config-only is an activation path that will
    eventually decide wrong.
    """
    if action in NEVER_PERMITTED:
        return False
    if action not in granted(level):
        return False
    if action == ACTIVATE_CONFIG:
        if str(change_kind or "").strip().lower() not in CONFIG_KINDS:
            return False
        if gates_all_passed is not True:
            # NOT `if not gates_all_passed`: None means nobody said, and unattended
            # activation on "nobody said" is the failure this rung is defined to avoid.
            return False
    return True


def require(level, action, *, change_kind=None, gates_all_passed=None, what="this step"):
    """Permit the action or raise, with the reason a reader needs.

    Exists so the check can sit at the sink. A permission function nothing calls is a
    description of a policy rather than a policy.
    """
    if action in NEVER_PERMITTED:
        raise AutonomyError("%s asked to %s: %s" % (what, action, NEVER_PERMITTED[action]))
    level = normalise(level)
    if action not in granted(level):
        need = next((r for r in LEVELS if action in _GRANTS.get(r, ())), None)
        raise AutonomyError(
            "%s asked to %s, which level %s does not permit%s"
            % (what, action, level,
               "; it is granted at level %s" % need if need else " at any level"))
    if action == ACTIVATE_CONFIG:
        kind = str(change_kind or "").strip().lower()
        if kind not in CONFIG_KINDS:
            raise AutonomyError(
                "%s asked to activate a %r change unattended; level C is typed/config-only "
                "because a typed parameter is reversible by writing the old value back, and "
                "that is what makes unattended activation recoverable"
                % (what, change_kind or "unspecified"))
        if gates_all_passed is not True:
            raise AutonomyError(
                "%s asked to activate unattended with gates_all_passed=%r; only True permits "
                "it -- 'nobody said' is not 'everything passed', and unattended is exactly "
                "where that difference stops being visible" % (what, gates_all_passed))
    return True


def raise_to(current, target, *, operator_approved=False, evidence=None) -> str:
    """Move up one rung, with an operator behind it. Returns the new level or raises.

    THIS IS NOT A MECHANISM, AND MUST NOT BE READ AS ONE. Nothing in production calls it, and
    that is deliberate rather than an omission waiting to be filled in. `operator_approved` is
    a boolean the CALLER asserts: any caller can pass True, including this system. Wiring it
    as it stands would convert an obvious stub into a control that looks enforced and is not,
    which is worse than the stub -- a later reader, or an audit, would take it for a working
    gate on autonomy.

    What it is: an executable statement of what an approval would have to contain. One rung at
    a time, an operator behind it, and evidence that can be produced later. Keep it, because
    the requirements are worth stating; do not call it until the assertion can come from
    somewhere this process cannot forge.

    See docs/research/governance_of_promotion.md: the honest ceiling on a single-machine
    design is that an agent holding a shell cannot be fenced out of its own guards, so what a
    protected key would buy is authenticity of the approval statement, never its enforcement.

    test_autonomy.py fails if a production caller appears, so this note cannot be bypassed by
    someone who did not read it.

    ONE RUNG AT A TIME. A to C would grant self-activation without the period in between --
    the system proposing, a human approving each winner, and someone able to see whether the
    proposals were any good. That period IS the evidence for the raise, so skipping it skips
    the evidence rather than merely the paperwork.
    """
    current, target = normalise(current), str(target or "").strip().upper()
    if target not in LEVELS:
        raise AutonomyError("%r is not a level; the ladder is %s" % (target, ", ".join(LEVELS)))
    here, there = LEVELS.index(current), LEVELS.index(target)
    if there <= here:
        raise AutonomyError("%s is not above %s; use lower_to to reduce autonomy"
                            % (target, current))
    if there - here > 1:
        raise AutonomyError(
            "%s to %s skips %s. The intervening rung is where the system proposes and a human "
            "approves each winner, which is the evidence that it should be trusted with the "
            "next one" % (current, target, ", ".join(LEVELS[here + 1:there])))
    if not operator_approved:
        raise AutonomyError(
            "raising %s to %s needs an operator. If the loop can raise its own level, every "
            "guard the level governs is advisory" % (current, target))
    if not str(evidence or "").strip():
        raise AutonomyError(
            "raising %s to %s cites no evidence; an approval nobody can account for later is "
            "indistinguishable from one nobody gave" % (current, target))
    return target


def lower_to(current, target) -> str:
    """Move down, no approval needed. Returns the new level or raises if it is not down.

    Deliberately asymmetric with `raise_to`. A tripwire at 2am should be able to put the
    system back to B without waiting for someone to wake up; a guard that can only be
    tightened by the person who is not there is a guard that stays loose.
    """
    current, target = normalise(current), str(target or "").strip().upper()
    if target not in LEVELS:
        raise AutonomyError("%r is not a level; the ladder is %s" % (target, ", ".join(LEVELS)))
    if LEVELS.index(target) >= LEVELS.index(current):
        raise AutonomyError("%s is not below %s; raising needs raise_to and an operator"
                            % (target, current))
    return target


def describe(level) -> dict:
    """What this level is and is not allowed to do -- for a status line or a run header.

    Reports the never-permitted actions alongside the granted ones. A status that lists only
    what is allowed reads, at level D, as though the remaining question is how much further
    there is to go.
    """
    level = normalise(level)
    return {
        "level": level,
        "grants": sorted(granted(level)),
        "never": sorted(NEVER_PERMITTED),
        "next": (LEVELS[LEVELS.index(level) + 1]
                 if LEVELS.index(level) + 1 < len(LEVELS) else None),
    }
