"""Where a piece of evidence came from, and what that entitles it to do.

THE FAILURE THIS PREVENTS

The brief calls it lineage poisoning, and it is the one threat specific to a system that
improves itself:

    malicious external content -> solver trajectory -> memory / failure analysis
        -> evolver -> harness mutation -> future CLEAN tasks affected

An ordinary prompt injection ends when the episode ends. This one does not. If text an
attacker controls can reach the store that primes future work, then a single contaminated
document rewrites how the agent behaves on tasks the attacker never touched -- and every
later run looks normal, because the instruction is now part of the harness rather than part
of the input.

The chain is not hypothetical in this repository. `relay.project_memory.record_task` writes
whatever a run produced, `load_notes` prepends it to future goals (relay_fleet ~line 2711),
and `memory` is the one component the evolution loop may actually change. Every link exists.

THE RULE, WHICH IS NARROWER THAN "DISTRUST EXTERNAL CONTENT"

External content must remain usable AS TASK DATA -- that is the product. What it must not do
is acquire authority over future policy. So authority is a property of the EVIDENCE, not of
the pipeline stage it happens to be sitting in:

    EXTERNAL_UNTRUSTED may influence the solution to the task it came from.
    It may never, by itself, justify a change to the harness.

`tools/_untrusted.py` already marks external content where it enters a turn. This module is
the other half: it keeps the mark attached to anything derived from that content, all the way
to the point where an experiment would be justified by it.
"""
from __future__ import annotations

#: Authority classes, ordered from most to least entitled. The order is meaningful --
#: `weakest_of` returns the least entitled class in a derivation, because evidence is only
#: as trustworthy as the least trustworthy thing it was derived from.
SYSTEM_POLICY = "SYSTEM_POLICY"
OPERATOR_INSTRUCTION = "OPERATOR_INSTRUCTION"
MACHINE_VERIFIER = "MACHINE_VERIFIER"
HUMAN_CORRECTION = "HUMAN_CORRECTION"
AGENT_INFERENCE = "AGENT_INFERENCE"
EXTERNAL_UNTRUSTED = "EXTERNAL_UNTRUSTED"
OCR_UNTRUSTED = "OCR_UNTRUSTED"
WEB_UNTRUSTED = "WEB_UNTRUSTED"
DOCUMENT_UNTRUSTED = "DOCUMENT_UNTRUSTED"

ORDER = (
    SYSTEM_POLICY,
    OPERATOR_INSTRUCTION,
    MACHINE_VERIFIER,
    HUMAN_CORRECTION,
    AGENT_INFERENCE,
    EXTERNAL_UNTRUSTED,
    OCR_UNTRUSTED,
    WEB_UNTRUSTED,
    DOCUMENT_UNTRUSTED,
)

#: The classes that may not, on their own, justify changing the harness. Note that
#: AGENT_INFERENCE is NOT here: an agent's own reasoning about its own run is a legitimate
#: input to improvement. What is excluded is anything an outsider wrote.
UNTRUSTED = frozenset({
    EXTERNAL_UNTRUSTED,
    OCR_UNTRUSTED,
    WEB_UNTRUSTED,
    DOCUMENT_UNTRUSTED,
})

#: What a machine actually checked. Evidence at this authority is the strongest kind an
#: automated loop can generate for itself, because it is a measurement rather than a claim.
VERIFIED = frozenset({SYSTEM_POLICY, OPERATOR_INSTRUCTION, MACHINE_VERIFIER, HUMAN_CORRECTION})


class ProvenanceError(ValueError):
    """Raised when evidence is asked to authorise something its origin does not permit."""


def normalise(authority) -> str:
    """An unknown or absent class is treated as EXTERNAL_UNTRUSTED, not as trusted.

    The direction of this default is the whole point. Evidence arriving with no provenance is
    evidence whose origin nobody recorded, and the safe reading of "we do not know where this
    came from" is not "it is fine".
    """
    value = str(authority or "").strip().upper()
    return value if value in ORDER else EXTERNAL_UNTRUSTED


def weakest_of(*authorities) -> str:
    """The least entitled class among these. Derivation cannot launder authority.

    A summary of an untrusted document is untrusted. A conclusion drawn from one untrusted
    source and three trusted ones is untrusted, because it could be wrong in exactly the way
    the untrusted source wanted. Taking the maximum instead is how a contaminated fact
    acquires trust by being mentioned alongside clean ones.
    """
    classes = [normalise(a) for a in authorities] or [EXTERNAL_UNTRUSTED]
    return max(classes, key=ORDER.index)


def may_justify_harness_change(authority) -> bool:
    """Is this evidence allowed to be a REASON for mutating the harness?

    Deliberately not "may this be recorded" or "may this be read". Untrusted content stays
    usable as task data -- that is the product working. This is only about the step where a
    piece of evidence becomes the justification for changing how future work is done.
    """
    return normalise(authority) not in UNTRUSTED


def require_authority_for_evolution(evidence, *, what="this change") -> str:
    """Return the effective authority, or raise if it cannot justify an evolution step.

    `evidence` is an iterable of provenance-tagged items -- dicts with an "authority" key, or
    bare authority strings. An EMPTY list raises: a proposal with no evidence at all has no
    provenance to check, and silently allowing it would make the whole mechanism optional by
    the simplest possible route.
    """
    items = list(evidence or [])
    if not items:
        raise ProvenanceError(
            "%s cites no evidence, so there is nothing whose origin can be checked; an "
            "unevidenced proposal is not made safe by having no provenance" % what)
    authorities = [it.get("authority") if isinstance(it, dict) else it for it in items]
    effective = weakest_of(*authorities)
    if not may_justify_harness_change(effective):
        raise ProvenanceError(
            "%s rests on %s evidence. External content may inform the task it came from; it "
            "may not authorise a change to the harness -- that is the step where one poisoned "
            "document becomes a permanent behaviour change on tasks it never touched."
            % (what, effective))
    return effective


def tag(item, authority):
    """Attach provenance to a dict without disturbing what is already there."""
    out = dict(item or {})
    out["authority"] = normalise(authority)
    return out
