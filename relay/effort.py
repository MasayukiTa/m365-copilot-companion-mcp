"""How hard to work on ONE goal, rather than on every goal in the run.

WHAT IS FIXED TODAY. `--effort` is read once in fleet_runner, turned into four run-level
numbers -- refuter on/off, max_refute, max_research, review_lenses -- and handed to
run_relay_fleet, which gives every worker the same four. A run of twenty goals where one is
hard and nineteen are arithmetic pays the hard one's price twenty times, or pays the cheap
price on the one that needed more. There is no third option, and the goal dict cannot express
one: goal_fields normalises a goal to (text, checks, cwd) and nothing else.

WHY THAT WAS RIGHT, AND WHERE IT STOPS BEING RIGHT. Effort modes were built to be orthogonal
to task type -- see the lens comment in relay/refuter.py: a non-coding task must never be
reviewed with code-specific criteria. Orthogonality is about WHICH lens, and it survives
per-goal effort untouched. What does not survive is uniformity: relay/fleet_runner.py already
records the cost of it -- "a UNIFORM ultra over-engineers easy tasks (observed: 44-47 line
diffs for 2-7 line gold fixes)". That observation is an argument for per-goal effort, and it
was answered by adding a fourth uniform mode instead.

WHAT THIS MODULE IS. The resolution rule only: given the run's effort and one goal, what does
that goal get. It reads nothing and runs nothing, so the rule can be tested without a fleet.
The run-level default is unchanged when a goal says nothing, which is every goal today.

WHAT IT IS NOT. It does not decide effort from the goal's content. That belongs with measured
competence (relay/selfimprove/calibration.recommend_effort already routes on measured pass@1
per class), and guessing difficulty from a goal's wording would be exactly the kind of
heuristic this repository has been burned by -- see the note in
feedback_deterministic_over_heuristic_design_flaw. A goal carries an effort because something
knew; nothing here invents one.
"""
from __future__ import annotations

#: The four knobs an effort level sets. Named here so a caller cannot silently add a fifth
#: on one path and not the other -- which is how the cockpit and the fleet came to disagree
#: about which outcomes were retryable.
KNOBS = ("refuter", "max_refute", "max_research", "review_lenses")

#: Effort levels and what each one means, mirroring fleet_runner's own branches. `lenses=None`
#: means a single general reviewer; a tuple means a panel; `()` with refuter False means none.
LEVELS = {
    "min":   {"refuter": False, "max_refute": 0, "max_research": 0, "review_lenses": None},
    "max":   {"refuter": True,  "max_refute": 1, "max_research": 3, "review_lenses": None},
    "auto":  {"refuter": True,  "max_refute": 3, "max_research": 3, "review_lenses": None},
    "ultra": {"refuter": True,  "max_refute": 4, "max_research": 6,
              "review_lenses": ("correctness", "edge", "security")},
}


def goal_effort(goal):
    """The effort level a goal asks for, or "" when it does not ask.

    Accepted in two places, because two kinds of caller write goals: a top-level "effort" for
    a person or a script composing a goals file, and metadata["effort"] for machinery that
    builds envelopes (fan-out children, retries) and keeps its own bookkeeping in metadata.
    """
    if not isinstance(goal, dict):
        return ""
    direct = goal.get("effort")
    if isinstance(direct, str) and direct.strip():
        return direct.strip().lower()
    meta = goal.get("metadata")
    if isinstance(meta, dict):
        value = meta.get("effort")
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


def resolve(goal, run_defaults, *, log=None):
    """The four knobs for ONE goal: its own level if it named a known one, else the run's.

    AN UNKNOWN LEVEL FALLS BACK AND SAYS SO. Silently treating "ultra2" as the run default
    would let a typo cost a goal its review while the goals file reads as though it asked for
    the most. The run default is still used -- refusing the goal outright would be a worse
    trade -- but the name that was not understood is reported.
    """
    out = dict(run_defaults or {})
    for knob in KNOBS:
        out.setdefault(knob, None)

    level = goal_effort(goal)
    if not level:
        return out
    if level not in LEVELS:
        if log:
            log("[effort] goal asked for %r, which is not one of %s; using the run's effort"
                % (level, ", ".join(sorted(LEVELS))))
        return out

    chosen = dict(LEVELS[level])
    lenses = chosen.get("review_lenses")
    chosen["review_lenses"] = list(lenses) if lenses else None
    return chosen


def describe(knobs):
    """One line, for a log that has to say what a worker was actually given."""
    lenses = knobs.get("review_lenses") or []
    return ("refuter=%s refute<=%s research<=%s lenses=%s"
            % (knobs.get("refuter"), knobs.get("max_refute"), knobs.get("max_research"),
               ",".join(lenses) if lenses else "-"))
