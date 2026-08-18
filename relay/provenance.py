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
INDEPENDENT_EVALUATOR = "INDEPENDENT_EVALUATOR"
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
    INDEPENDENT_EVALUATOR,
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


# --------------------------------------------------------------------------------------------
# Adjudication: which claim wins when two of them disagree about the same fact.
# --------------------------------------------------------------------------------------------
#
# THREE ORDERS, AND CONFUSING THEM IS THE BUG THIS SECTION EXISTS TO AVOID.
#
#   `ORDER`           -- TRUST. Who is allowed to direct the system. An operator outranks a
#                        verifier here, and rightly so: the operator says what the system is
#                        for.
#   `EVIDENCE_ORDER`  -- OBSERVATION. Whose account of what actually happened is believed.
#                        The operator does not appear in it at all.
#   `weakest_of`      -- DERIVATION, and it runs the other way. A conclusion is only as strong
#                        as its weakest premise, so there the weakest input wins.
#
# The first draft of this section reused `ORDER` and was wrong in the exact way the brief
# warns about: "consider this task finished" (OPERATOR_INSTRUCTION) beat "the file does not
# exist" (MACHINE_VERIFIER), because in the trust order the operator is higher. An instruction
# states what should be; it does not observe what is. Ranking a policy above a measurement
# lets the harness be told that it succeeded.


#: Whose account of a FACT is believed, strongest first -- the brief's order: machine
#: final-state verification > human correction > independent evaluator > solver
#: self-evaluation. Untrusted sources are present but last. They are ranked rather than
#: omitted so a claim carrying one is resolved (and loses) instead of being treated as an
#: unknown authority, which would be a quieter and more confusing outcome.
EVIDENCE_ORDER = (
    MACHINE_VERIFIER,
    HUMAN_CORRECTION,
    INDEPENDENT_EVALUATOR,
    AGENT_INFERENCE,
    EXTERNAL_UNTRUSTED,
    OCR_UNTRUSTED,
    WEB_UNTRUSTED,
    DOCUMENT_UNTRUSTED,
)

#: Authorities that say what SHOULD be true rather than reporting what IS. They top the trust
#: order and have no place in the evidence order; a claim tagged with one is refused as
#: evidence of fact rather than silently ranked, because both alternatives are bad -- ranking
#: it high lets an instruction overrule a measurement, and ranking it low silently demotes the
#: operator, which the next reader would reasonably call a bug.
NORMATIVE = frozenset({SYSTEM_POLICY, OPERATOR_INSTRUCTION})


def _same_value(a, b) -> bool:
    """Whether two claimed values are the same, without trusting `__repr__` to say so.

    `repr` comparison was the first draft and it fails in both directions: two dicts built in
    different orders read as a conflict that is not there, and two distinct objects sharing a
    terse `__repr__` read as agreement that is not there. The second is the dangerous one --
    it hides a conflict -- so equality is tried first, and `repr` is only the fallback for
    values that refuse to compare.
    """
    try:
        result = a == b
        if isinstance(result, bool):
            return result
    except Exception:
        pass
    try:
        return repr(a) == repr(b)
    except Exception:
        return False    # a value whose repr raises is not evidence that it matches anything


def _evidence_rank(authority) -> int:
    # The fallback is unreachable today: `normalise` already turns anything it does not
    # recognise into EXTERNAL_UNTRUSTED, which is in this tuple. It stays as a second line
    # of defence -- if a future authority is added to ORDER and forgotten here, the sane
    # failure is "it wins nothing", not "it silently ranks first by landing at index 0".
    try:
        return EVIDENCE_ORDER.index(normalise(authority))
    except ValueError:
        return len(EVIDENCE_ORDER)


def outranks(a, b) -> bool:
    """Whether authority `a`'s account of a FACT is believed over `b`'s.

    Not the trust order -- see the note at the top of this section. Normative and unknown
    authorities outrank nothing here, including each other.
    """
    if normalise(a) in NORMATIVE or normalise(b) in NORMATIVE:
        return False
    ra, rb = _evidence_rank(a), _evidence_rank(b)
    if ra == len(EVIDENCE_ORDER):
        return False
    return ra < rb


def adjudicate(claims, *, fact=None) -> dict:
    """Resolve competing claims about ONE fact. Returns the winner AND the disagreement.

    `claims` is a sequence of {"authority", "value", ...}; anything else on each claim is
    carried through untouched. An optional `"fact"` on each claim names what it is about.

    THE DISAGREEMENT IS THE POINT, not a footnote. Quietly returning the machine's answer
    would be correct and would also discard the most valuable thing in the input: that the
    solver said otherwise. "The verifier and the solver disagreed" is the signal this whole
    benchmark exists to catch, and a resolver that hides it makes the system look consistent
    at exactly the moments it is not.

    THREE THINGS IT REFUSES TO DO, each because answering would be worse than not:

      * claims about DIFFERENT facts are not a conflict -- they are a caller error, and
        picking a winner between them would be a confident answer to a question nobody asked;
      * a NORMATIVE authority is not evidence of what happened, so it cannot win;
      * two claims at the SAME authority that disagree are not resolved. Two verifiers
        reporting different values means something is wrong with the verifiers, and taking
        the first, the newest, or the majority would paper over it.

    An unresolved result carries `value=None` and `resolved=False`. Read it through
    `resolved_value` at the point a decision is taken, so an unresolved conflict cannot be
    mistaken for a `None` answer.
    """
    rows = [c for c in (claims or []) if isinstance(c, dict)]
    if not rows:
        return {"winner": None, "value": None, "conflict": False, "resolved": False,
                "claims": [], "reason": "no claims"}

    facts = {c.get("fact") for c in rows if c.get("fact") is not None}
    if fact is not None:
        facts.add(fact)
    if len(facts) > 1:
        return {"winner": None, "value": None, "conflict": False, "resolved": False,
                "claims": rows,
                "reason": ("these claims are about different facts (%s), so they do not "
                           "disagree and there is nothing to adjudicate"
                           % ", ".join(sorted(repr(f) for f in facts)))}

    normative = [c for c in rows if normalise(c.get("authority")) in NORMATIVE]
    factual = [c for c in rows if normalise(c.get("authority")) not in NORMATIVE]
    if not factual:
        return {"winner": None, "value": None, "conflict": False, "resolved": False,
                "claims": rows, "refused_as_normative": normative,
                "reason": ("every claim carries a normative authority (%s), which states what "
                           "should be rather than what was observed; there is no evidence here"
                           % ", ".join(sorted({normalise(c.get("authority"))
                                               for c in normative})))}

    ranked = sorted(factual, key=lambda c: _evidence_rank(c.get("authority")))
    best = ranked[0]
    best_rank = _evidence_rank(best.get("authority"))
    tied = [c for c in ranked if _evidence_rank(c.get("authority")) == best_rank]

    if any(not _same_value(c.get("value"), best.get("value")) for c in tied):
        return {
            "winner": None, "value": None, "conflict": True, "resolved": False,
            "authority": normalise(best.get("authority")), "claims": rows, "tied": tied,
            "reason": ("%d claims at the same authority (%s) disagree; that points at the "
                       "claimants rather than at the fact, and picking one would hide it"
                       % (len(tied), normalise(best.get("authority")))),
        }

    overruled = [c for c in factual if not _same_value(c.get("value"), best.get("value"))]
    out = {
        "winner": best, "value": best.get("value"), "conflict": bool(overruled),
        "resolved": True, "authority": normalise(best.get("authority")), "claims": rows,
        "reason": "%s decides" % normalise(best.get("authority")),
    }
    if normative:
        # Recorded, never ranked: a reader needs to see that an instruction was present and
        # was not allowed to settle a question of fact.
        out["refused_as_normative"] = normative
    if overruled:
        out["overruled"] = overruled
        out["reason"] = (
            "%s decides, overruling %s -- a measurement of the final state is a different "
            "kind of thing from a claim about it"
            % (normalise(best.get("authority")),
               ", ".join(sorted({normalise(c.get("authority")) for c in overruled}))))
    return out


def resolved_value(result, *, what="this decision"):
    """The adjudicated value, or raise if the conflict was not resolved.

    Why this exists rather than callers reading `result["value"]`: an unresolved conflict
    carries `value=None`, and `None` is a plausible-looking answer. A gate reading the field
    directly would treat "the verifiers contradicted each other" as "nothing was found" --
    the failure the refusal was meant to prevent, reintroduced one line later.
    """
    if not (result or {}).get("resolved"):
        raise ProvenanceError("%s rests on an unresolved conflict: %s"
                              % (what, (result or {}).get("reason") or "no claims"))
    return result["value"]
