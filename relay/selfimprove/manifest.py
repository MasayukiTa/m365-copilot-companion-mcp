"""The harness as an explicit, versioned configuration surface.

Until now "the harness" meant whatever the code happened to do that week, plus a handful of
environment toggles. That is unmeasurable in the specific sense that matters: two runs
cannot be compared because there is no statement of what differed between them.

A manifest names the components and their versions, plus the parameters that are tuned
rather than written. Hashing its canonical form gives a `harness_id` -- the thing an
experiment cites, an archive row keys on, and a later reader uses to ask "what was actually
running".

WHAT MAY EVOLVE, AND WHAT MAY NEVER

The allowlist is the load-bearing part of this file. A self-improvement loop optimises
whatever it is allowed to touch, and a security check is, from the optimiser's point of
view, a source of failures to be removed. It will not be malicious about it; it will simply
notice that the runs where the check is weaker score better.

So the forbidden set is enumerated explicitly and checked at the point of resolution, not
left to the good sense of whoever writes the next proposer. It is not "we would never"; it
is "the call raises".
"""
from __future__ import annotations

import hashlib
import json

SCHEMA_VERSION = 1

# Components the evolver may propose new versions of. Everything here is policy or
# configuration -- how much context to keep, when to ask for review, how many times to
# retry -- none of it decides whether an action is permitted.
#
# A COMPONENT BELONGS HERE ONLY ONCE SOMETHING DISPATCHES ON IT. All seven of the original
# entries were labels: an independent review found not one production call site, so swapping
# planner/v1 for planner/v2 changed the manifest hash and ran identical code -- both arms of
# the A/B were the same program and the p-value described noise. Aspirational entries are
# worse than absent ones, because an experiment over them looks valid and cannot be.
#
# `memory` earns its place: relay/project_memory.MEMORY_VERSIONS maps each version to a
# different selection strategy. The other six are listed in UNIMPLEMENTED_COMPONENTS below,
# where they document intent without licensing an experiment.
EVOLVABLE_COMPONENTS = frozenset({
    "memory",
    # PROMOTED 2026-08-20, in the two steps this comment block prescribes: the version table
    # (relay.quality_cards.QUALITY_CARDS_VERSIONS) was written and shown to dispatch
    # differently -- v2 suppresses, replaces and appends cards from the applied genome -- and
    # only then was the name moved. Until that table existed the genome carried a `cards`
    # field that NOTHING read, so every card A/B ran the same program twice.
    "quality_cards",
})

#: Named so the intent survives, and so a future implementer knows where to look -- but NOT
#: evolvable, because there is nothing behind them yet. Moving one up is a two-part edit:
#: write the version table, then move the name.
UNIMPLEMENTED_COMPONENTS = frozenset({
    "planner",
    "reviewer",
    "retry",
    "context",
})

# Never evolvable, at any autonomy level. These decide what is ALLOWED, or they are the
# judge. A system that can tune its own judge has no measurement, and one that can tune its
# own permission checks has no boundary -- and in both cases the resulting numbers still
# look fine, which is what makes it dangerous rather than merely wrong.
FORBIDDEN_COMPONENTS = frozenset({
    "security",            # tools/security.py -- the unlock/permission gate
    "untrusted",           # tools/_untrusted.py -- external-content distrust semantics
    "frozen",              # relay/selfimprove/frozen.py -- integrity of the judge
    "grader",              # any grader implementation
    "sealed_answers",      # the holdout
    "permissions",
    "authorization",
    "autonomy",            # relay/selfimprove/autonomy.py -- the ladder that decides which
                           # guards apply. If the loop can move its own rung, every guard the
                           # rung governs becomes advisory in a single step.
    "provenance",          # authority classes; see the brief's lineage-poisoning section
})

#: Only the components that exist. The six aspirational names moved to
#: UNIMPLEMENTED_COMPONENTS: carrying them here made every manifest advertise seven knobs
#: while six of them dispatched to nothing.
DEFAULT_COMPONENTS = {
    "memory": "memory/v1",
    "quality_cards": "quality_cards/v1",
}

#: Every parameter's type and the range a run may actually use. The upper bounds are not
#: arbitrary tidiness: max_retries or the refuter budget set to a million is a candidate that
#: never finishes, which arrives as an infra abort -- an outcome a candidate now has reasons
#: to prefer. A range is the cheapest way to keep "the experiment ran" true.
PARAMETER_TYPES = {
    "max_refute_passes": (0, 10),
    "max_retries": (0, 50),
    "memory_max_items": (0, 100),
}

# Every parameter here MUST have a production reader. An independent review found three of
# four had none, which means an A/B over them ran the same program twice and reported a
# p-value about noise. max_context_budget was removed rather than wired: there is no context
# budgeter in this harness to tune, and a knob with nothing behind it is worse than a
# missing knob -- it invites experiments that cannot possibly measure anything.
DEFAULT_PARAMETERS = {
    "max_refute_passes": 2,       # -> relay_fleet: how many refuter passes a candidate gets
    # -> relay_fleet: how many times an AGENT-REPORTED STUCK is re-prompted before the
    # worker goes terminal. Transport failures (send, turn timeout) are NOT bounded by
    # this -- they ride out NET_RETRY_WINDOW_S, because a short count exhausted during a
    # brief outage and ended every worker.
    #
    # It said "transient-retry budget per worker" and was reaching nothing but a display
    # string: `max_transient` survived only inside "retry %d/%d", which printed "3/2"
    # against a limit that limited nothing. A genome coordinate with no effect is worse
    # than a missing one -- the loop tunes it, measures noise, and can KEEP the result.
    #
    # 10, NOT 3 -- the value run_relay_fleet had in its signature before the manifest
    # became the source of it. Writing 3 would silently cut the budget to a third for
    # every production run, with nothing to see in a diff of the fleet. THE BASE
    # MANIFEST MUST REPRODUCE CURRENT PRODUCTION EXACTLY, or adopting it is itself an
    # unreviewed change to the product.
    "max_retries": 10,
    "memory_max_items": 5,        # -> project_memory: how much history is primed into a goal
}


class ManifestError(ValueError):
    """Raised when a manifest names something it must not, or is malformed."""


def canonical(manifest: dict) -> str:
    """Deterministic serialisation. The id is only stable if this is."""
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def harness_id(manifest: dict) -> str:
    return hashlib.sha256(canonical(manifest).encode("utf-8")).hexdigest()


def base_manifest() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "components": dict(DEFAULT_COMPONENTS),
        "parameters": dict(DEFAULT_PARAMETERS),
    }


def known_versions(component: str) -> frozenset:
    """The versions a component actually dispatches on, read from the implementation.

    Read rather than declared, so the allowlist and the dispatch table cannot disagree --
    a second copy of this list is a second thing to forget to update.
    """
    if component == "memory":
        try:
            from relay.project_memory import MEMORY_VERSIONS
            return frozenset(MEMORY_VERSIONS)
        except Exception:
            return frozenset()
    if component == "quality_cards":
        try:
            from relay.quality_cards import QUALITY_CARDS_VERSIONS
            return frozenset(QUALITY_CARDS_VERSIONS)
        except Exception:
            return frozenset()
    return frozenset()


def validate(manifest: dict) -> None:
    """Raise unless the manifest is well-formed AND touches nothing forbidden."""
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be a dict")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError("unsupported schema_version: %r" % manifest.get("schema_version"))
    components = manifest.get("components")
    if not isinstance(components, dict):
        raise ManifestError("manifest.components must be a dict")
    for name in components:
        if name in FORBIDDEN_COMPONENTS:
            raise ManifestError(
                "component %r is not evolvable: it decides what is permitted, or it is the "
                "judge. A loop that can tune either still produces numbers that look fine."
                % name)
        if name not in EVOLVABLE_COMPONENTS:
            raise ManifestError("unknown component %r; add it to EVOLVABLE_COMPONENTS "
                                "deliberately rather than by accident" % name)
        # A VERSION NOTHING IMPLEMENTS IS THE NO-OP CANDIDATE AGAIN. Only the component NAME
        # was checked, so "memory/does-not-exist" validated, changed the harness id, and then
        # fell back to memory/v1 at runtime -- a different manifest running identical code,
        # which is the exact defect the version table was introduced to eliminate. Checked
        # against the dispatch table itself so the two cannot drift.
        known = known_versions(name)
        if known and components[name] not in known:
            raise ManifestError(
                "component %r has no implementation for version %r (known: %s); a version "
                "nothing dispatches on changes the harness id and not the behaviour"
                % (name, components[name], ", ".join(sorted(known))))
    params = manifest.get("parameters")
    if not isinstance(params, dict):
        raise ManifestError("manifest.parameters must be a dict")
    # THE SAME NO-OP DEFECT ONE LEVEL DOWN. Component versions were validated and parameter
    # VALUES were not, so {"memory_max_items": "not-an-integer"} validated, changed the
    # harness id, and ran with the default five -- a different manifest executing identical
    # code, which is the thing this module exists to prevent. And 5 / 5.0 / "5" hashed three
    # ways while behaving one way, so an A/B could compare a genome with itself.
    for name, value in params.items():
        if name not in PARAMETER_TYPES:
            raise ManifestError("unknown parameter %r; add it to DEFAULT_PARAMETERS with a "
                                "production reader rather than by accident" % name)
        low, high = PARAMETER_TYPES[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ManifestError(
                "parameter %r must be an int, got %r -- a value the runtime cannot use "
                "changes the harness id and not the behaviour" % (name, value))
        if not (low <= value <= high):
            raise ManifestError("parameter %r out of range [%d, %d]: %r"
                                % (name, low, high, value))


def apply_genome(base: dict, genome: dict) -> dict:
    """Produce a NEW manifest with the genome's overrides applied.

    `base` is never mutated: a candidate that edits the manifest it was derived from
    destroys the ability to compare it against its own parent.
    """
    validate(base)
    out = {
        "schema_version": base["schema_version"],
        "components": dict(base["components"]),
        "parameters": dict(base["parameters"]),
    }
    for name, version in (genome or {}).get("components", {}).items():
        if name in FORBIDDEN_COMPONENTS:
            raise ManifestError("genome tried to change forbidden component %r" % name)
        if name not in EVOLVABLE_COMPONENTS:
            raise ManifestError("genome names unknown component %r" % name)
        out["components"][name] = version
    for key, value in (genome or {}).get("parameters", {}).items():
        if key not in DEFAULT_PARAMETERS:
            raise ManifestError("genome names unknown parameter %r" % key)
        out["parameters"][key] = value
    validate(out)
    return out


def diff(a: dict, b: dict) -> dict:
    """What changed between two manifests, as {field: (from, to)}.

    An experiment that cannot state its own diff in one line cannot be attributed, and
    "the genome changed" is not a statement anybody can act on later.
    """
    out = {}
    for section in ("components", "parameters"):
        for key in sorted(set(a.get(section, {})) | set(b.get(section, {}))):
            before, after = a.get(section, {}).get(key), b.get(section, {}).get(key)
            if before != after:
                out["%s.%s" % (section, key)] = (before, after)
    return out
