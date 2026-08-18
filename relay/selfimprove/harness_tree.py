"""Phase 10: one harness per task class, and the reasons that is mostly a bad idea.

THE IDEA

Different work wants different settings. A long-running orchestration job benefits from a
generous retry budget; a one-shot document edit is better served by failing fast and telling
someone. A single global manifest has to compromise between them, and a tree lets each task
class carry its own.

WHY IT IS GUARDED THIS HEAVILY

Per-class configuration multiplies the number of things that can be true at once, and every
one of the guarantees built in the preceding phases is a statement about a SINGLE harness:
the frozen set, the attestation, the paired comparison, the archive row that says which
harness produced a number. A tree quietly turns each of those from a fact into a fact-per-
class, and the failure is silent -- everything still runs, and "which harness was that?"
stops having an answer.

So the tree here is deliberately shallow and deliberately boring:

  * two levels only -- a root and one override per task class. No inheritance chains, because
    a value three levels up is a value nobody will find while debugging;
  * an override may only touch coordinates the root already declares, so a class cannot
    introduce a component the allowlist has not seen;
  * every resolution is EXPLAINED -- resolve() returns which class matched and which fields
    the override supplied, because a configuration you cannot account for is one you cannot
    reproduce;
  * and an unknown task class resolves to the root rather than to nothing, so a new kind of
    work runs under the reviewed configuration instead of an accidental one.

WHAT IT DOES NOT DO

It does not learn the tree. Which classes deserve their own harness is a question the
campaign answers by finding a genome that wins for one class and loses for another, and that
evidence has to exist before the tree gets a branch. Growing branches automatically would
produce a per-class configuration fitted to whatever noise each class happened to contain.
"""
from __future__ import annotations

from relay.selfimprove import manifest as M


#: The task classes a branch may be named for -- the brief's list, as a CLOSED set.
#:
#: Closed because `resolve` is public and takes a string. If any string could name a branch,
#: a caller that derived a class from task content would reach a branch directly and the
#: routing rules in `relay.selfimprove.routing` would be advisory. With the vocabulary shut,
#: the worst such a caller can do is pick among branches an operator declared -- still worth
#: preventing, which is what routing is for, but no longer an open door.
TASK_CLASSES = (
    "coding",
    "spreadsheet",
    "document",
    "ocr",
    "sql",
    "research",
    "long_running_local",
    "m365_cloud",
    "security_sensitive",
)


class TreeError(ValueError):
    """Raised when a tree would make the running configuration unaccountable."""


def validate(tree: dict) -> None:
    """Raise unless the tree is well-formed and every override is legal on its own.

    Each resolved manifest is validated in full rather than the overrides being checked
    piecewise: the question that matters is whether the thing that will RUN is legal, and an
    override can be individually innocuous and jointly wrong.
    """
    if not isinstance(tree, dict):
        raise TreeError("a harness tree must be a dict")
    root = tree.get("root")
    if not isinstance(root, dict):
        raise TreeError("a harness tree needs a root manifest; without one there is no "
                        "reviewed configuration to fall back to")
    M.validate(root)

    overrides = tree.get("overrides") or {}
    if not isinstance(overrides, dict):
        raise TreeError("overrides must be {task_class: genome}")

    for task_class, genome in overrides.items():
        if not str(task_class).strip():
            raise TreeError("a task class must be named")
        if str(task_class).strip() not in TASK_CLASSES:
            raise TreeError(
                "%r is not one of the task classes a branch may be named for. An open "
                "vocabulary lets a class derived from task content name a branch, which "
                "makes the routing rules advisory rather than enforced" % task_class)
        for section in ("components", "parameters"):
            for key in (genome.get(section) or {}):
                if key not in (root.get(section) or {}):
                    raise TreeError(
                        "override %r introduces %s.%s, which the root does not declare -- a "
                        "branch may tune what the root has, not add to it"
                        % (task_class, section, key))
        # the thing that will actually run
        M.apply_genome(root, genome)


def resolve(tree: dict, task_class: str) -> dict:
    """The manifest for this task class, and an account of how it was reached.

    Returns {"manifest", "matched", "overrides", "harness_id"}. The account is not decoration:
    with a tree in place, "which harness produced this number" has a different answer per
    class, and every archive row and attestation downstream depends on being able to say it.
    """
    validate(tree)
    root = tree["root"]
    overrides = tree.get("overrides") or {}
    key = str(task_class or "").strip()

    if key in overrides:
        genome = overrides[key]
        manifest = M.apply_genome(root, genome)
        return {"manifest": manifest, "matched": key,
                "overrides": M.diff(root, manifest),
                "harness_id": M.harness_id(manifest)}

    # AN UNKNOWN CLASS GETS THE ROOT, not an error and not an empty manifest. New kinds of
    # work appear constantly, and the safe thing for one to run under is the configuration a
    # human reviewed.
    return {"manifest": root, "matched": None, "overrides": {},
            "harness_id": M.harness_id(root)}


def branches(tree: dict) -> list:
    """Every distinct harness this tree can produce, with what makes it distinct.

    A tree with five branches that resolve to three manifests is worth knowing about: two of
    those branches are documentation rather than configuration.
    """
    validate(tree)
    seen = {}
    for task_class in [None] + sorted(tree.get("overrides") or {}):
        got = resolve(tree, task_class or "")
        seen.setdefault(got["harness_id"], {"harness_id": got["harness_id"],
                                            "classes": [], "overrides": got["overrides"]})
        seen[got["harness_id"]]["classes"].append(task_class or "(root)")
    return list(seen.values())


def justified(tree: dict, evidence) -> dict:
    """Which branches the evidence actually supports, and which are guesses.

    `evidence` maps a task class to the decision states its per-class candidate reached. A
    branch earns its place when a change was KEPT for that class; a branch nobody measured is
    a per-class configuration fitted to whatever that class happened to contain, which is the
    failure mode this whole phase risks.
    """
    validate(tree)
    out = {"justified": [], "unjustified": []}
    for task_class in sorted(tree.get("overrides") or {}):
        states = list((evidence or {}).get(task_class) or [])
        if "KEEP" in states:
            out["justified"].append(task_class)
        else:
            out["unjustified"].append(task_class)
    return out
