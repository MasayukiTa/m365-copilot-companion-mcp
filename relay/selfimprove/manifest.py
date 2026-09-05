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
    # PROMOTED 2026-09-05, in the two steps this comment block prescribes. The version table
    # (relay.relay_fleet.SKILL_VERSIONS) was written first and shown to dispatch differently
    # -- skill/v1 composes the matched procedure ahead of the goal, skill/off leaves the goal
    # exactly as it was -- and only then was the name moved here.
    #
    # It earns a place because the claim behind it is untested. Frame-side matching was built
    # on a measurement of the FAILURE it replaces: skill_match, 178 calls, 145 dead on a
    # guessed argument name, 33 successes in the whole ledger. That the procedure now ARRIVES
    # is measured. That arriving HELPS is not -- it costs 1.4-7.8 KB of a worker's first turn,
    # and only an arm that turns it off can price that.
    #
    # The arms differ in the injection and in nothing else: matching, the marker handling and
    # the approval request to a human are identical on both sides, so a comparison does not
    # also compare how much got approved along the way.
    "skill",
    # PROMOTED 2026-08-20, in the two steps this comment block prescribes: the version table
    # (relay.quality_cards.QUALITY_CARDS_VERSIONS) was written and shown to dispatch
    # differently -- v2 suppresses, replaces and appends cards from the applied genome -- and
    # only then was the name moved. Until that table existed the genome carried a `cards`
    # field that NOTHING read, so every card A/B ran the same program twice.
    "quality_cards",
    # PROMOTED 2026-08-20, same two steps: planner.PLANNER_VERSIONS was written and shown to
    # dispatch differently -- v1 opens with the goal, v2 opens with the plan prompt in front
    # of it -- before the name was moved. plan_mode, the operator's plan-then-wait flag, is
    # deliberately NOT what this selects; an arm that waits for a person cannot be one side
    # of an unattended comparison.
    "planner",
    # PROMOTED 2026-08-21. NOT the same thing as `routing`, which stays forbidden three
    # blocks down, and the difference is a property of the code rather than of the idea:
    #
    #   a routing decision selects the HARNESS a task runs under, so the checks differ
    #   between branches and an optimiser learns to steer toward the lax one;
    #   a transport decision selects only how the conversation is CARRIED. Manifest,
    #   graders, folder policy and unlock boundary are identical on both sides, so getting
    #   it wrong buys memory and never permission.
    #
    # THE INVARIANT THIS RESTS ON, stated so a future argument has to meet it rather than
    # appeal to the name: the two transports must remain identical in permissions and
    # checks. The day the socket side skips a consent step the tab performs, or accepts on a
    # different rule, transport selection IS routing selection and belongs back below.
    #
    # And it rests on two files being frozen: route_evaluator.py, which holds the REJECT rule
    # this family is judged by, and socket_route.py, which holds the circuit breaker, the
    # one-way fallback counter and the label writer. A classifier that could relax those
    # would make "route more to sockets" free.
    #
    # What may move is thresholds and the eligible-kind list. The Work IQ predicate is code,
    # because its errors are SILENT -- a socket answer formed without Work IQ context can
    # reach DONE with no fallback, and the label then says the socket was fine.
    "transport",
})

#: Named so the intent survives, and so a future implementer knows where to look -- but NOT
#: evolvable, because there is nothing behind them yet. Moving one up is a two-part edit:
#: write the version table, then move the name.
UNIMPLEMENTED_COMPONENTS = frozenset({
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
    # ROUTING IS HERE BY DECISION, not because it is unimplemented -- and it spent a few hours
    # in neither set because I deleted it by accident while promoting quality_cards, which
    # turned "declared, deliberately not evolvable" into "unknown name". Nothing opened: an
    # unknown component is still refused. What was lost is the REASON, which is the whole job
    # of these three sets.
    #
    # Forbidden rather than a future promotion: routing.py's own opening says a routing
    # decision is a choice of configuration, and anything that can influence the choice can
    # influence the configuration. Point an optimiser at that and the pressure is to learn a
    # classifier that steers work toward the branch with the laxest checks -- which reads as a
    # performance gain, because from outside that is what it looks like. Same shape as tuning
    # the grader. That ROUTING_AUTHORITIES, SAFE_FEATURES and `at_least_as_strict` exist at
    # all is this repository already conceding a routing decision can be an escalation.
    "routing",
})

#: Only the components that exist. The six aspirational names moved to
#: UNIMPLEMENTED_COMPONENTS: carrying them here made every manifest advertise seven knobs
#: while six of them dispatched to nothing.
DEFAULT_COMPONENTS = {
    "memory": "memory/v1",
    "skill": "skill/v1",          # the behaviour in place; skill/off is the comparison
    "quality_cards": "quality_cards/v1",
    "planner": "planner/v1",
    "transport": "transport/v1",
}

#: Every parameter's type and the range a run may actually use. The upper bounds are not
#: arbitrary tidiness: max_retries or the refuter budget set to a million is a candidate that
#: never finishes, which arrives as an infra abort -- an outcome a candidate now has reasons
#: to prefer. A range is the cheapest way to keep "the experiment ran" true.
PARAMETER_TYPES = {
    "max_refute_passes": (0, 10),
    "max_research": (0, 20),
    "max_retries": (0, 50),
    "memory_max_items": (0, 100),
    "review_lens_count": (0, 3),
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
    # -> relay_fleet: how many on-demand research side-pages a worker may open.
    # 3, because that is what run_relay_fleet's signature said before this line existed.
    "max_research": 3,
    # -> relay_fleet: how many of PANEL_LENSES review a candidate. 0 = no panel.
    #
    # 0, AND THE ZERO IS THE WHOLE POINT. A panel is what `ultra` means, and `ultra` is not
    # what a run does unless it is asked for. Any other value here would turn every
    # production run into a three-reviewer run on the commit that added the knob -- the
    # exact failure the max_retries comment above records, in the expensive direction.
    #
    # WHY THIS EXISTS AT ALL. The benchmark harness could not express `ultra`: the child it
    # spawns passes `refuter` and deliberately leaves the rest unset so the manifest supplies
    # them, and the manifest had no lens knob. So `auto` and `ultra` resolved to the same
    # program, ran as the same program, and were recorded under the SAME harness_id -- which
    # means no comparison between them has ever been made through this scorecard, whatever
    # anyone remembers concluding. A knob here is what makes the two arms different programs
    # and, because the id is a hash of this dict, different harnesses on the record.
    "review_lens_count": 0,
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
    if component == "skill":
        try:
            from relay.relay_fleet import SKILL_VERSIONS
            return frozenset(SKILL_VERSIONS)
        except Exception:
            return frozenset()
    if component == "quality_cards":
        try:
            from relay.quality_cards import QUALITY_CARDS_VERSIONS
            return frozenset(QUALITY_CARDS_VERSIONS)
        except Exception:
            return frozenset()
    if component == "planner":
        try:
            from relay.planner import PLANNER_VERSIONS
            return frozenset(PLANNER_VERSIONS)
        except Exception:
            return frozenset()
    if component == "transport":
        try:
            from relay.transport_policy import TRANSPORT_VERSIONS
            return frozenset(TRANSPORT_VERSIONS)
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


def materialize(genome, base=None):
    """Turn a genome into the harness it actually produces: (manifest, harness_id).

    WHY THIS IS IN THE CONSTITUTION AND NOT A HELPER SOMEWHERE

    Two genomes can be different objects with different ids and produce the SAME PROGRAM. A
    genome that names a parameter at its default value -- {"parameters": {"max_retries": 10}}
    against a base whose max_retries is already 10 -- has its own genome_id and materialises
    to a manifest byte-identical to the base's. Measured, not argued: genome_id 7a9b1bb314fe
    and 21a42e331dc2 both give harness_id 942eb26c19d2f138a688.

    Every place that asks "are these two the same?" has to ask it here. Asking it of genome ids
    lets an A/A comparison through wearing two names, which is the one thing an experiment may
    never be, and it is the defect this repository has now found in five separate components.
    """
    manifest = apply_genome(base or base_manifest(), genome or {})
    return manifest, harness_id(manifest)


#: The only genome keys a manifest models. Anything else -- the archive's `knobs`/`cards`
#: scaffold vocabulary, for instance -- is invisible here.
MANIFEST_KEYS = ("components", "parameters")

#: Keys that carry lineage or prose rather than behaviour, and so do not make a genome
#: unrepresentable.
INERT_KEYS = ("parent_id", "note", "id")


def represented_by_manifest(genome) -> bool:
    """True iff everything this genome can change is something a manifest models.

    THE ANSWER FROM `same_program` IS ONLY MEANINGFUL WHEN THIS IS TRUE.

    `apply_genome` reads `components` and `parameters` and nothing else, so two genomes that
    differ ONLY in `cards` -- the archive's scaffold vocabulary -- materialise to the same
    manifest while being genuinely different candidates. Asking `same_program` about them
    returns True and means "the manifest cannot see the difference", not "there is none".

    Found by measurement, not by reading: wiring same_program into the proposer's no-op filter
    without this guard dropped EVERY candidate in two existing tests, real mutations included,
    because their genomes speak the scaffold vocabulary.
    """
    if not isinstance(genome, dict):
        return False
    return all(k in MANIFEST_KEYS or k in INERT_KEYS for k in genome)


def same_program(genome_a, genome_b, base=None) -> bool:
    """True iff both genomes produce the same harness AND the manifest can see them both.

    Returns False when either genome carries something the manifest does not model: "I cannot
    tell" must not be reported as "they are the same", because the caller uses this to DROP a
    candidate, and a false positive there silently empties the proposal.
    """
    if not (represented_by_manifest(genome_a) and represented_by_manifest(genome_b)):
        return False
    return materialize(genome_a, base)[1] == materialize(genome_b, base)[1]
