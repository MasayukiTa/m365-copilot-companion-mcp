"""Which role and protocol definitions the fleet uses today, and a content hash of each.

READ FROM WHERE THEY ARE DEFINED. Nothing is copied here: every part below is looked up by
module and attribute at call time, so the hash is of the text a worker is actually given, and an
edit to a prompt in its own module changes the hash without anyone touching this file.

TWO KINDS, matching slices D1 and D2.
  roles       who an agent is for one kind of turn: the planner, the splitter, the refuter with its
              lenses, the aggregator (merge).
  workflows   the protocol text every worker receives and the follow-up framings the runner
              adds: what the conversation is told to do and how it must end.

VERSION AND HASH. `hash` is the content: sha256 of the canonical JSON of the parts, so any
character change in a prompt gives a new hash. `version` is the label of how the definition is
COMPOSED (which parts it has). It is bumped by hand when a part is added or removed, never for a
wording change, so a prompt edit by someone else cannot turn CI red. Two runs with the same
version and different hashes ran different wording of the same design.

THE AGGREGATOR IS BUILT BY A FUNCTION, not stored as a constant, so it is hashed as the text the
function renders for a fixed fixture that exercises both branches (a finished and a missing
slice). A change to the template changes the hash; a change to a comment does not.

A PART THAT CANNOT BE READ (module fails to import, attribute renamed) leaves the definition with
an empty hash and the part named in `missing`. It is never skipped silently and never hashed as
if it were absent, because two sets that differ only in an unreadable part would then compare
equal.

THIS MODULE IS OUTSIDE THE FROZEN SET and is not part of any harness_id.
"""
from __future__ import annotations

import hashlib
import importlib
import json

HASH_LEN = 16

#: Fixture for the aggregator template. Fixed text, not a goal.
_AGG_GOAL = "(fixture goal)"
_AGG_RESULTS = (
    {"subtask_index": 1, "outcome": "DONE", "result": "(fixture result)"},
    {"subtask_index": 2, "outcome": "MISSING", "result": ""},
)


def _attr(module, name):
    def read():
        return getattr(importlib.import_module(module), name)
    read.label = "%s.%s" % (module, name)
    return read


def _aggregator_text():
    from relay import fanout
    return fanout.aggregation_prompt(_AGG_GOAL, [dict(r) for r in _AGG_RESULTS])


_aggregator_text.label = "relay.fanout.aggregation_prompt(fixture)"

#: name -> (version of the composition, parts)
ROLES = {
    "planner": ("v1", (_attr("relay.planner", "PLAN_PROMPT"),
                       _attr("relay.planner", "APPROVE_JOB"))),
    "splitter": ("v1", (_attr("relay.fanout", "SPLIT_JOB"),
                        _attr("relay.fanout", "MIDRUN_SPLIT_JOB"))),
    "refuter": ("v1", (_attr("relay.refuter", "REFUTER_INSTRUCTION"),
                       _attr("relay.refuter", "SOURCE_CHECK_RULE"),
                       _attr("relay.refuter", "REFUTER_NUDGE"),
                       _attr("relay.refuter", "LENS_PROMPTS"),
                       _attr("relay.refuter", "UNVERIFIABLE_PREAMBLE"))),
    "aggregator": ("v1", (_aggregator_text,)),
}

#: PROTOCOL already contains SKILL_SENTENCE and the output discipline, so it is the one part.
WORKFLOWS = {
    "worker_protocol": ("v1", (_attr("relay.copilot_autopilot_relay", "PROTOCOL"),)),
    "follow_up": ("v1", (_attr("relay.fleet_runner", "FOLLOW_UP_PROMPT"),
                         _attr("relay.fleet_runner", "SYSTEM_RECOVERY_FOLLOW_UP_PROMPT"))),
}


def _canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=list)


def _norm(value):
    """Line endings normalised so a Windows checkout and Linux CI hash the same text."""
    if isinstance(value, str):
        return value.replace("\r\n", "\n")
    if isinstance(value, dict):
        return {str(k): _norm(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    return value


def _digest(obj) -> str:
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()[:HASH_LEN]


def hash_definition(parts) -> dict:
    """{"hash": hex or "", "missing": [labels]} for one definition. Never raises."""
    got, missing = {}, []
    for part in parts:
        label = getattr(part, "label", repr(part))
        try:
            got[label] = _norm(part())
        except Exception:
            missing.append(label)
    return {"hash": "" if missing else _digest(got), "missing": missing}


def _section(table) -> dict:
    out = {}
    for name in sorted(table):
        version, parts = table[name]
        entry = {"version": version}
        entry.update(hash_definition(parts))
        if not entry["missing"]:
            del entry["missing"]
        out[name] = entry
    return out


def _set_hash(section) -> str:
    """Hash of a whole section; empty when any definition in it could not be read."""
    if any(not e.get("hash") for e in section.values()):
        return ""
    return _digest({n: [e["version"], e["hash"]] for n, e in section.items()})


def snapshot() -> dict:
    """{"roles": {...}, "workflows": {...}, "roles_hash": hex, "workflows_hash": hex}.

    Pure reads. Calling it twice with unchanged source gives equal results."""
    roles, workflows = _section(ROLES), _section(WORKFLOWS)
    return {"roles": roles, "workflows": workflows,
            "roles_hash": _set_hash(roles), "workflows_hash": _set_hash(workflows)}


def genotype_id(snap) -> str:
    """One label for the organisation version a run ran under, derived from a snapshot.

    The fleet already carries roles_hash and workflows_hash as a pair; this folds that pair
    into a single deterministic id so a run can be named by one value instead of two. It reads
    only the snapshot it is given and reads nothing else, sets nothing and does not touch a run.

    EMPTY MEANS UNREADABLE, NOT A VERSION. When either half is empty -- a part of the roles or
    the workflows could not be read, so _set_hash returned "" -- the id is empty too. A run
    whose definitions could not be read must not share an id with one whose could, which is what
    would happen if the empty half were hashed as if it were a real value."""
    roles_hash = str((snap or {}).get("roles_hash") or "")
    workflows_hash = str((snap or {}).get("workflows_hash") or "")
    if not roles_hash or not workflows_hash:
        return ""
    return _digest([roles_hash, workflows_hash])
