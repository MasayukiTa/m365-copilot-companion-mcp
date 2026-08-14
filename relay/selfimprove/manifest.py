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
EVOLVABLE_COMPONENTS = frozenset({
    "memory",
    "planner",
    "reviewer",
    "retry",
    "context",
    "routing",
    "quality_cards",
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
    "provenance",          # authority classes; see the brief's lineage-poisoning section
})

DEFAULT_COMPONENTS = {
    "memory": "memory/v1",
    "planner": "planner/v1",
    "reviewer": "reviewer/v1",
    "retry": "retry/v1",
    "context": "context/v1",
    "routing": "routing/v1",
    "quality_cards": "quality/v1",
}

DEFAULT_PARAMETERS = {
    "max_context_budget": 18000,
    "review_threshold": 0.35,
    "max_retries": 3,
    "memory_max_items": 5,
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
    params = manifest.get("parameters")
    if not isinstance(params, dict):
        raise ManifestError("manifest.parameters must be a dict")


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
