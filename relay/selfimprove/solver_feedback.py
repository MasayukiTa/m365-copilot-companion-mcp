"""Ask the solver what got in its way -- Phase 6 of the brief.

WHAT THIS IS FOR, AND WHY A TRAJECTORY IS NOT ENOUGH. A transcript shows what the solver did.
It does not show what it looked for and could not find, which tool it avoided because the
last three calls were awkward, or which piece of recalled memory sent it the wrong way. Those
are the things that limit a harness, and the only party that observed them is the solver.

NOT GROUND TRUTH, AND THE MODULE IS BUILT AROUND THAT. A solver complaining that a tool is
confusing may be wrong; it may have misread the tool, or be rationalising a failure of its
own. The brief says this generates HYPOTHESES and must not decide acceptance, so:

  * nothing here returns a verdict, a score, or anything a gate reads;
  * every item carries the episode it came from, so a claim can be checked against what
    actually happened rather than believed;
  * `to_hypotheses` emits proposals for the ledger -- which is the thing that then has to
    survive a paired evaluation like any other.

A NOTE ON WHY IT IS SEPARATE FROM `harness_feedback`. That module observes the LOOP's own
health -- whether the proposer's hypotheses survive, whether the archive is covering ground.
This one observes the SOLVER's experience of the harness. Both are feedback and they are
about different subjects; the names are close enough that this paragraph exists so the next
reader does not merge them.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter


def enabled() -> bool:
    """Whether a runner should ask the solver for feedback after an episode's reply. Off.

    Off by default, and deliberately: the question this module asks is a follow-up TURN, and
    a follow-up turn costs time and -- for a stateful adapter -- may itself become part of
    what the episode's history looks like from here on. That is a change to what the
    benchmark measures, not a free observation, so it stays opt-in behind an environment
    toggle exactly like `relay.settle.unified()`. Whoever turns this on for a run must also
    be able to see that they did, which is why the flag is listed in
    `relay.selfimprove.experiment.FINGERPRINT_ENV_KEYS` -- an unrecorded toggle is an
    unrecorded confound.

    Read from the environment on every call rather than captured at import, so the value a
    run actually used is the value the fingerprint records.
    """
    return os.environ.get("MCP_SOLVER_FEEDBACK", "0").strip() == "1"


#: The shape asked for in the brief. Every field is a list except the last two, which are
#: free text -- kept exactly as specified so a solver prompted from the brief produces
#: something this can read.
LIST_FIELDS = (
    "missing_information",
    "unnecessary_context",
    "tool_friction",
    "missing_tool_capability",
    "confusing_instruction",
    "memory_useful",
    "memory_harmful",
)
TEXT_FIELDS = ("review_overhead", "suggested_harness_change")
FIELDS = LIST_FIELDS + TEXT_FIELDS

#: How many items one episode may contribute per field. A solver that returns forty
#: complaints is not forty times as informative as one that returns two, and letting it
#: dominate the tally turns "which friction is common" into "which solver was chattiest".
MAX_ITEMS_PER_FIELD = 10

#: How long a single item may be. Long enough for a sentence, short enough that the reply is
#: a report rather than a second transcript.
MAX_ITEM_CHARS = 300


def prompt() -> str:
    """The question to put to the solver. Kept here so every caller asks the same thing."""
    return (
        "Answer with JSON only, no prose around it. What got in your way on this task?\n"
        "%s\n"
        "Every list may be empty -- an empty answer is a real answer and is preferred to a "
        "guess. Do not describe what you did; that is already recorded. Describe what you "
        "needed and did not have, what wasted your attention, and what you would change "
        "about the tools or instructions."
        % json.dumps({**{k: [] for k in LIST_FIELDS},
                      **{k: "" for k in TEXT_FIELDS}}, indent=2)
    )


def parse(reply: str) -> dict:
    """Pull the structured answer out of a solver's reply. Never raises.

    Tolerant of the wrappers models put around JSON -- a fenced block, a sentence before it --
    because the alternative is discarding real feedback over punctuation. What it will not do
    is guess: anything it cannot read comes back as empty fields plus `parse_error`, which is
    honest and countable, rather than a partially-invented answer.
    """
    empty = {k: [] for k in LIST_FIELDS}
    empty.update({k: "" for k in TEXT_FIELDS})
    text = (reply or "").strip()
    if not text:
        return dict(empty, parse_error="empty reply")

    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return dict(empty, parse_error="no JSON object in the reply")
    try:
        data = json.loads(text[start:end + 1])
    except Exception as exc:
        return dict(empty, parse_error="%s: %s" % (type(exc).__name__, exc))
    if not isinstance(data, dict):
        return dict(empty, parse_error="the JSON was not an object")

    out = dict(empty)
    for key in LIST_FIELDS:
        value = data.get(key)
        if isinstance(value, str):
            value = [value] if value.strip() else []
        if not isinstance(value, list):
            continue
        items = []
        for item in value[:MAX_ITEMS_PER_FIELD]:
            item = str(item).strip()[:MAX_ITEM_CHARS]
            if item:
                items.append(item)
        out[key] = items
    for key in TEXT_FIELDS:
        value = data.get(key)
        out[key] = str(value).strip()[:MAX_ITEM_CHARS] if value is not None else ""
    unknown = sorted(set(data) - set(FIELDS))
    if unknown:
        # Recorded rather than dropped silently: a solver inventing fields is a sign the
        # prompt drifted from what the reader expects, and that is worth seeing.
        out["unexpected_fields"] = unknown
    return out


def collect(episode_id: str, reply: str) -> dict:
    """One episode's feedback, tagged with where it came from.

    The tag is not bookkeeping. An item that cannot be traced to an episode cannot be checked
    against what happened there, and unverifiable feedback treated as fact is the failure mode
    the brief warns about.
    """
    return {"episode_id": episode_id, **parse(reply)}


def tally(entries) -> dict:
    """What several episodes said, counted by how many EPISODES raised each item.

    Counted per episode rather than per mention: one solver repeating itself is one
    observation, and totalling raw mentions would let a single verbose reply outvote five
    quiet ones. The count is the whole reason to aggregate -- an item raised once is an
    anecdote, and an item raised on half the suite is a property of the harness.
    """
    per_field = {}
    for field in LIST_FIELDS:
        counter = Counter()
        where = {}
        for entry in entries or []:
            for item in set(entry.get(field) or []):
                counter[item] += 1
                where.setdefault(item, []).append(entry.get("episode_id"))
        per_field[field] = [
            {"item": item, "episodes": count, "where": sorted(x for x in where[item] if x)}
            for item, count in counter.most_common()
        ]
    return {
        "episodes": len(entries or []),
        "parse_errors": sum(1 for e in (entries or []) if e.get("parse_error")),
        "fields": per_field,
        "suggestions": [e["suggested_harness_change"] for e in (entries or [])
                        if (e.get("suggested_harness_change") or "").strip()],
    }


def to_hypotheses(tallied, *, min_episodes=2) -> list:
    """Turn recurring friction into proposals -- never into decisions.

    `min_episodes` defaults to 2 because one episode's complaint is an anecdote and this
    module's whole job is to avoid dressing anecdotes as findings. The output is a list of
    things to TEST: each carries its own evidence and the episodes it came from, so the
    paired evaluation that follows can be checked against the claim that prompted it.

    Nothing here decides anything. A hypothesis from a solver complaint has to survive the
    same gate as one from anywhere else, and if it does not survive, the complaint was wrong
    -- which is a legitimate outcome and the reason feedback is evidence rather than truth.
    """
    out = []
    for field, items in (tallied.get("fields") or {}).items():
        for row in items:
            if row["episodes"] < min_episodes:
                continue
            out.append({
                "target_failure_class": field,
                "hypothesis": ("solvers reported %r on %d episodes; if that is a real harness "
                               "limit rather than a rationalisation, addressing it should "
                               "change the outcome on those episodes"
                               % (row["item"], row["episodes"])),
                "evidence_episodes": row["where"],
                "raised_by": row["episodes"],
                # NOT A GENOME. This module cannot know which knob answers a complaint, and
                # guessing one here would smuggle a decision into a report. A human or a
                # proposer picks the change; this says only what to aim at.
                "genome": None,
            })
    return sorted(out, key=lambda h: -h["raised_by"])


# ---------------------------------------------------------------------------------------
# WHERE, for the quality-diversity map -- Phase 7's missing axis, populated from Phase 6.
# ---------------------------------------------------------------------------------------

#: Which harness component each feedback field points at. Phase 7 asks the archive to be
#: organised around reusable failure PATHOLOGY, and the existing axes say what KIND of failure
#: occurred (functional, side-effect, security) rather than WHERE in the harness it came from.
#: "a functional failure" is not actionable; "the memory component is recalling the wrong
#: things" is, and it is the link between the archive and the component sweep -- an archive
#: that cannot say which component to work on cannot direct Phase 5.
#:
#: The mapping is deliberately narrow. Only fields that point at ONE component appear:
#: `suggested_harness_change` is free text about anything, and guessing a component from it
#: would manufacture attribution rather than read it.
FIELD_TO_COMPONENT = {
    "missing_information": "context",
    "unnecessary_context": "context",
    "tool_friction": "tool",
    "missing_tool_capability": "tool",
    "confusing_instruction": "instruction",
    "memory_useful": "memory",
    "memory_harmful": "memory",
    "review_overhead": "reviewer",
}

#: What an episode gets when its feedback names no component -- which is the common case, and
#: must stay visibly separate from "the harness was fine". A map that silently folded silence
#: into a real cell would report attribution it does not have, and the archive already has a
#: written-up incident of exactly that: descriptors resolving to a default collapsed every row
#: into one cell, and a map with one cell reports maximum quality and no diversity.
UNATTRIBUTED = "unattributed"


def where(entry) -> str:
    """Which harness component this episode's feedback points at, or `unattributed`.

    ONE component or none. Feedback naming two different components is not evidence that
    either is at fault -- it is evidence that the solver had several complaints, and picking
    the first or the loudest would invent a finding. Ties go to `unattributed` for the same
    reason a coin landing on its edge is not heads.

    `memory_useful` is deliberately in the mapping alongside `memory_harmful`: the axis is
    WHERE the harness made a difference, not where it went wrong, and a memory component that
    is carrying an episode is as much a fact about that component as one that is misleading.
    """
    named = set()
    for field, component in FIELD_TO_COMPONENT.items():
        value = (entry or {}).get(field)
        if isinstance(value, str):
            value = [value] if value.strip() else []
        if value:
            named.add(component)
    return named.pop() if len(named) == 1 else UNATTRIBUTED


def where_distribution(entries) -> dict:
    """How the episodes distribute over components, `unattributed` included.

    Reported rather than hidden: if most episodes are unattributed then the WHERE axis is not
    carrying information yet, and a reader needs to see that before treating the few attributed
    cells as a map of the harness.
    """
    counts = Counter(where(e) for e in (entries or []))
    total = sum(counts.values())
    return {
        "counts": dict(counts),
        "attributed": total - counts.get(UNATTRIBUTED, 0),
        "total": total,
        "attribution_rate": round((total - counts.get(UNATTRIBUTED, 0)) / total, 4)
        if total else None,
    }
