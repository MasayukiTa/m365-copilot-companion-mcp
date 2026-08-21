"""Open-ended PROPOSE step for the self-improvement loop (L4 design, section 3 + 7).

L1's "propose" was implicit -- a human wrote a quality card by hand. L4 makes proposal a *generative*
step: given the partitioned *real* misses (infra already filtered out) and the genome archive, a
meta-agent invents candidate scaffold genomes (new card texts, and -- eventually -- new mechanisms).

KEY DESIGN: this module is the *disciplined harness* around a candidate GENERATOR, not the generator
itself. The actual invention (an LLM) is injected as a `generate_fn` callable, so:

  - the discipline (section 3 + 7) is enforced here REGARDLESS of what the generator returns;
  - the module is fully testable without any model -- a deterministic placeholder generator
    (`mutation_generator`) stands in for the LLM in tests and as a safe fallback.

The discipline is the whole point. A generated candidate is DROPPED unless it is:
  (a) domain-general          -- every card text passes guards.overfit_lint (section 3);
  (b) not a dead idea         -- its genome_id is not in rejected_ids (anti ideation-fatigue, the
                                 SICA lesson, section 3 "diversity requirement");
  (c) novel                   -- its genome_id is not already in the archive (already tried);
  (d) not a no-op             -- it differs from its parent genome (no identity mutation);
  (e) unique within the batch -- no two survivors share a genome_id.

PROPOSE is the writer; it NEVER approves. The independent approver is the frozen significance_gate
(section 0 / section 7 "enforcing" guards). This module imports guards/archive READ-ONLY.

stdlib only; deterministic; no model calls in this module (the model is the injected generate_fn).
"""
from __future__ import annotations

from typing import Callable, Iterable

from relay.selfimprove import guards as _guards
from relay.selfimprove.archive import Archive, genome_id

# --------------------------------------------------------------------------------------------------
# Lint helper -- combine the overfit violations across all of a genome's card texts
# --------------------------------------------------------------------------------------------------


def lint_candidate(genome: dict) -> list[str]:
    """Return the combined overfit violations across a genome's card texts (empty == clean).

    A genome is domain-general iff none of its card texts name a concrete repo/instance/file/test
    (guards.overfit_lint). Knobs are env-var toggles and are not free text, so only card *values*
    are linted. Violations are prefixed with the card name so a caller can see which card leaked,
    and de-duplicated while preserving order.
    """
    cards = (genome or {}).get("cards") or {}
    seen: set[str] = set()
    out: list[str] = []
    for name in sorted(cards):
        for v in _guards.overfit_lint(str(cards[name] or "")):
            tagged = "%s:%s" % (name, v)
            if tagged not in seen:
                seen.add(tagged)
                out.append(tagged)
    return out


# --------------------------------------------------------------------------------------------------
# PROPOSE -- the disciplined harness around an injected generator
# --------------------------------------------------------------------------------------------------


def propose_candidates(
    real_misses: Iterable[dict],
    archive: Archive,
    generate_fn: Callable[[list, dict | None, int], list],
    *,
    rejected_ids: Iterable[str] | None = None,
    n: int = 3,
    parent_strategy: str = "qd",
    metric: str = "pass_at_1",
) -> list[dict]:
    """Propose up to `n` disciplined candidate genomes built on a chosen archive parent.

    Steps:
      1. pick a parent via archive.select_parent(parent_strategy, metric). May be None when the
         archive is empty -> the generator is told parent=None, meaning "start from the base".
      2. raw = generate_fn(real_misses, parent, n) -> a list of candidate genome dicts, each shaped
         like an archive genome: {"knobs", "cards", "parent_id", "note"}.
      3. DISCIPLINE FILTER (section 3 + 7) -- drop a candidate if ANY of:
           (a) any card text fails overfit_lint (lint_candidate non-empty)      -> not domain-general
           (b) its genome_id is in rejected_ids                                 -> dead idea
           (c) its genome_id is already in the archive                          -> already tried
           (d) its genome_id == genome_id(parent) when parent is not None       -> no-op mutation
           (e) its genome_id duplicates an earlier survivor in this batch       -> intra-batch dup
      4. stamp lineage: each survivor's "parent_id" is set to the parent's id (or None).

    Returns the survivors (<= n), in the order the generator produced them.
    """
    rejected = set(rejected_ids or ())

    parent_entry = archive.select_parent(parent_strategy, metric)
    # select_parent returns an archive ENTRY ({"id", "genome", ...}); the generator wants the genome.
    parent_genome = parent_entry.get("genome") if parent_entry else None
    parent_gid = genome_id(parent_genome) if parent_genome is not None else None

    raw = list(generate_fn(list(real_misses), parent_genome, n) or [])

    survivors: list[dict] = []
    batch_ids: set[str] = set()
    for cand in raw:
        if not isinstance(cand, dict):
            continue
        gid = genome_id(cand)
        if lint_candidate(cand):          # (a) not domain-general
            continue
        if gid in rejected:               # (b) dead idea
            continue
        if archive.get(gid) is not None:  # (c) already tried
            continue
        # (d) NO-OP MUTATION, DECIDED ON THE HARNESS AND NOT ON THE ID.
        #
        # This compared genome ids, and two different ids can produce the same program: a
        # genome that names a parameter at its default value has its own id and materialises
        # to a manifest byte-identical to the one it "changed". Measured: genome ids
        # 7a9b1bb314fe and 21a42e331dc2 both give harness 942eb26c19d2f138a688. Such a
        # candidate passed this filter, went to the evaluator, and was scored as an
        # experiment whose two arms were the same program.
        if parent_gid is not None and _same_program(cand, parent_entry):
            continue
        if gid in batch_ids:              # (e) intra-batch duplicate
            continue
        # stamp lineage (a fresh dict so we never mutate the generator's object identity surprisingly)
        stamped = dict(cand)
        stamped["parent_id"] = parent_gid
        survivors.append(stamped)
        batch_ids.add(gid)
        if len(survivors) >= n:
            break
    return survivors


# --------------------------------------------------------------------------------------------------
# Default deterministic generator -- a NO-LLM placeholder for tests and as a safe fallback
# --------------------------------------------------------------------------------------------------

# A small fixed set of GENERIC, env-gated scaffold knobs (toggled, never instance-specific). These are
# the kind of domain-general policy switches section 3 calls "a different turn budget policy" etc.
_GENERIC_KNOBS = (
    "SS_SELFTEST",        # run a self-test pass before declaring DONE
    "SS_VERIFY_OUTPUT",   # check exact output, not merely "does not crash"
    "SS_EXTRA_TURNS",     # allow a larger turn budget
    "SS_REPRO_FIRST",     # reproduce the symptom before editing
)

# A SMALL LIBRARY of GENERIC, domain-general card templates. NONE names a repo / instance / source
# file / test -- each is written to pass guards.overfit_lint (asserted in test_propose). These are the
# placeholder "inventions"; a real LLM generate_fn would synthesise richer mechanisms from the misses.
_GENERIC_CARDS = (
    ("trace_symptom",
     "Trace the reported symptom through the code path before editing; change the smallest scope that "
     "removes the cause."),
    ("verify_exact_output",
     "Verify the exact expected output and edge cases, not just that the code runs without raising."),
    ("reproduce_first",
     "Reproduce the failing behaviour first, then confirm your change actually flips it."),
    ("minimal_diff",
     "Prefer the minimal correct change; avoid unrelated refactors that widen the blast radius."),
)


def mutation_generator(real_misses, parent: dict | None, n: int) -> list[dict]:
    """Deterministic, NO-LLM candidate generator: a placeholder for an LLM generate_fn.

    Produces simple DOMAIN-GENERAL mutations of `parent` (or of an empty base when parent is None):
    candidate i toggles one generic env knob ON and appends one generic card, both selected by index
    so the output is fully deterministic -- no random, no time, no instance/repo/file/test names. The
    same (parent, n) always yields the same genomes, so the discipline filter and the loop are
    replayable.

    NOTE: this is a stand-in. The real PROPOSE meta-agent (section 3) injects an LLM here that invents
    new card texts and mechanisms from the partitioned real misses; this fallback exists so the
    harness is testable and degrades safely when no model is wired up.
    """
    base_knobs = dict((parent or {}).get("knobs") or {})
    base_cards = dict((parent or {}).get("cards") or {})

    out: list[dict] = []
    for i in range(max(0, int(n))):
        knobs = dict(base_knobs)
        knob = _GENERIC_KNOBS[i % len(_GENERIC_KNOBS)]
        knobs[knob] = "1"

        cards = dict(base_cards)
        card_name, card_text = _GENERIC_CARDS[i % len(_GENERIC_CARDS)]
        cards[card_name] = card_text

        out.append({
            "knobs": knobs,
            "cards": cards,
            "parent_id": None,   # propose_candidates stamps the real lineage
            "note": "mutation_generator placeholder: toggle %s + card %s" % (knob, card_name),
        })
    return out


def _same_program(candidate, parent_entry, base=None):
    """True iff the candidate materialises to the harness its parent already is.

    Falls back to id equality when the parent's genome is not available -- a weaker check, but
    the alternative is admitting everything when the archive row is thin.
    """
    from relay.selfimprove import manifest as M
    parent_genome = (parent_entry or {}).get("genome")
    if not isinstance(parent_genome, dict):
        return False
    try:
        return M.same_program(candidate, parent_genome, base)
    except Exception:
        return False
