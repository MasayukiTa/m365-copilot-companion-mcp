"""Component-wise optimisation: vary one thing, hold the rest, and combine the winners.

THE SHAPE, AND WHY IT IS THIS ONE

The brief asks for a coordinate sweep rather than a joint search: for each component, hold
the others fixed, generate variants, evaluate, archive the winner, move on -- then combine
the individual winners and evaluate THAT as its own candidate.

The reason is attribution, which is the same reason everything else in this package is shaped
the way it is. A joint search over four knobs produces a winning tuple and no statement about
which knob did the work, and every later decision has to treat that tuple as indivisible. A
coordinate sweep is slower and answers a question.

WHY THE COMBINATION IS EVALUATED RATHER THAN ASSUMED

Individual winners do not compose. Memory that recalls more and a retry budget that gives up
sooner may each help alone and fight each other together, and the archive would happily
record a combination nobody ever ran as the best known harness. So the combined genome is a
CANDIDATE like any other -- proposed, evaluated, and rejected if it does not beat its parent.

WHAT THIS DOES NOT DO

It does not decide when to stop, and it does not run unattended. Every candidate goes through
EvolutionController, which means every one of the gates applies: a hypothesis written first,
frozen-set checks either side, security and sentinel requirements, and activation off unless
the operator asked for it. This module chooses what to TRY; nothing here can keep anything.
"""
from __future__ import annotations

from relay import provenance as PROV
from relay.selfimprove import manifest as M
from relay.selfimprove.controller import EvolutionController


def variants_for(component_or_parameter: str, base=None) -> list:
    """Every value worth trying for one coordinate, holding the rest of the manifest fixed.

    Read from the same places the validator reads, so a variant that would be refused at
    apply_genome time is never generated: the version table for a component, the declared
    range for a parameter. A generator that can propose an invalid candidate spends a whole
    evaluation slot discovering what a lookup would have said.
    """
    base = base or M.base_manifest()
    name = component_or_parameter

    if name in M.EVOLVABLE_COMPONENTS:
        current = base["components"].get(name)
        return [{"components": {name: v}}
                for v in sorted(M.known_versions(name)) if v != current]

    if name in M.PARAMETER_TYPES:
        low, high = M.PARAMETER_TYPES[name]
        current = base["parameters"].get(name)
        # A small, deliberately coarse ladder. Fine steps over a noisy benchmark produce
        # candidates whose difference is smaller than the measurement can resolve, and each
        # one costs a full paired run to learn nothing.
        candidates = sorted({max(low, min(high, v)) for v in
                             (low, current // 2 if current else low, current,
                              (current or 1) * 2, high)})
        return [{"parameters": {name: v}} for v in candidates if v != current]

    raise ValueError("not an evolvable coordinate: %r" % name)


def coordinates(agent=None) -> list:
    """The coordinates to sweep, components before parameters.

    Components first because a version change is a behavioural change and a parameter is a
    dial on whatever behaviour is in place; tuning the dial before choosing the mechanism
    measures the old mechanism.

    FILTERED BY WHAT THE AGENT'S TARGET CAN ACTUALLY EXERCISE. The first real sweep spent
    eight of its thirteen slots on max_retries and max_refute_passes against an in-process
    agent, and paired_evaluate refused all eight -- correctly, since that target does not read
    them. The refusals were the contract working; generating the candidates anyway was this
    module doing exactly what its own docstring warns against, which is discovering by
    evaluation what a lookup would have said.
    """
    covered = frozenset(getattr(agent, "covered_fields", ()) or ()) if agent else None
    coords = sorted(M.EVOLVABLE_COMPONENTS) + sorted(M.PARAMETER_TYPES)
    if covered is None:
        return coords
    return [c for c in coords
            if ("components.%s" % c) in covered or ("parameters.%s" % c) in covered]


#: What a sweep's proposals rest on, stated rather than left to a default. A coordinate sweep
#: enumerates the manifest's own declared parameter ranges and measures each one; no external
#: text enters the choice. The controller no longer fills this in for an absent caller -- it
#: cannot see the caller's reasoning, and an assertion made on someone else's behalf was
#: exactly the hole that let untrusted content reach a harness change by saying nothing.
SWEEP_EVIDENCE = ({
    "kind": "coordinate_enumeration",
    "authority": PROV.AGENT_INFERENCE,
    "note": "the candidates are the manifest's own declared ranges, enumerated; the decision "
            "between them is this loop's measurement of its own runs",
},)


def sweep(controller: EvolutionController, evaluate, *, base=None, coords=None,
          agent=None, hypothesis_for=None, on_result=None, evidence=None) -> dict:
    """One pass over the coordinates. Returns the winners and every decision reached.

    `evaluate` is the controller's evaluator, unchanged. `hypothesis_for(coord, genome)`
    supplies the prediction that has to exist before a candidate runs -- the ledger refuses a
    proposal without one, and this module does not invent them, because a generated
    hypothesis is a sentence rather than a prediction.

    The COMBINED genome is run as a candidate like any other when more than one coordinate
    wins. An earlier version built it and returned it unevaluated, which meant the one genome
    most likely to be adopted was the only one in the sweep nobody had measured -- and two
    changes that each helped alone are precisely the pair that can fight each other.
    """
    base = base or M.base_manifest()
    evidence = list(evidence or SWEEP_EVIDENCE)
    results, winners = [], {}

    def _run(genome, coord):
        out = controller.run_candidate(
            genome=genome,
            hypothesis=(hypothesis_for or _default_hypothesis)(coord, genome),
            target_failure_class=coord,
            evaluate=evaluate,
            evidence=evidence,
            base=base,
        )
        row = {"coordinate": coord, "genome": genome,
               "state": out["decision"]["state"], "reason": out["decision"]["reason"],
               "effect": _effect(out)}
        results.append(row)
        if on_result is not None:
            on_result(row)
        return row

    for coord in (coords or coordinates(agent)):
        kept = []
        for genome in variants_for(coord, base):
            row = _run(genome, coord)
            # KEEP is the only state that may win a coordinate. INCONCLUSIVE is the common
            # outcome and explicitly does not win: carrying an unproven change forward as a
            # winner is how a sweep accumulates noise and calls it progress.
            if row["state"] == "KEEP":
                kept.append(row)
        if kept:
            # THE BEST MEASURED ONE, not the last enumerated one. Two variants of a
            # coordinate can both be kept, and taking whichever happened to come last makes
            # the winner a function of dict ordering rather than of the measurement.
            winners[coord] = max(kept, key=lambda r: r["effect"])["genome"]

    combined, combined_row = None, None
    if len(winners) > 1:
        combined = _combine(winners)
        combined_row = _run(combined, "combined")
        if combined_row["state"] != "KEEP":
            # The parts won and the whole did not. That is a real finding and the reason the
            # combination is not installed on the strength of its parts.
            combined = None

    return {"winners": winners, "results": results, "combined": combined,
            "combined_decision": combined_row}


def _effect(out) -> float:
    """How much a kept candidate actually moved, for ranking two winners of one coordinate.

    Falls back to 0.0 rather than guessing: an unranked winner should tie, not invent a lead.
    """
    for key in ("effect", "delta", "improvement"):
        value = (out.get("measurements") or {}).get(key)
        if isinstance(value, (int, float)):
            return float(value)
    value = (out.get("decision") or {}).get("effect")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _combine(winners) -> dict:
    """The union of the per-coordinate winners, as a single genome to be EVALUATED.

    Never installed on the strength of its parts. Two changes that each helped alone can
    fight each other, and an archive that recorded a combination nobody ran as the best known
    harness would be recording a guess in the place where measurements go.
    """
    genome = {"components": {}, "parameters": {}}
    for g in winners.values():
        genome["components"].update(g.get("components") or {})
        genome["parameters"].update(g.get("parameters") or {})
    return {k: v for k, v in genome.items() if v}


def _default_hypothesis(coord, genome):
    value = (genome.get("components") or genome.get("parameters") or {})
    return ("changing %s to %r should reduce failures attributable to %s"
            % (coord, list(value.values())[0] if value else "?", coord))
