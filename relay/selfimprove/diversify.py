"""N-diversity genome generator -- the INPUT side of best-of-N (bench/AGENT_STRENGTHS.md, Bet #1).

best-of-N only pays off if the N attempts actually DIFFER. A best-of-N run that fires N identical
scaffolds is just one attempt billed N times. This module turns ONE strong known scaffold (the
incumbent / base genome) into N DIVERSE genomes -- one per attempt -- so the N solves explore N
different scaffold tweaks instead of re-running the same thing.

Design (mirrors the PROPOSE discipline in propose.py, reused READ-ONLY):
  - attempt 0 is ALWAYS the base genome itself (the strong incumbent -- never gamble away the known
    good scaffold);
  - attempts 1..N-1 are DISTINCT domain-general mutations of the base, produced deterministically by
    relay.selfimprove.propose.mutation_generator;
  - every returned genome is DOMAIN-GENERAL (each card passes guards.overfit_lint, reusing
    propose.lint_candidate), has a DISTINCT genome_id (dedupe by id, base included), is NOT a dead
    idea (genome_id not in rejected_ids), and was NOT already tried (genome_id not in a passed Archive);
  - if the generator cannot reach N distinct domain-general variants, we return as many as we can
    (>= 1, always including base) -- best-of-N then simply runs with fewer attempts. We NEVER pad with
    duplicates, because a duplicate attempt buys no diversity.

Deterministic (mutation_generator is deterministic; we vary only by index). stdlib only; no random,
no time, no network, no subprocess. Imports propose / archive / guards READ-ONLY.
"""
from __future__ import annotations

from relay.selfimprove import propose as _propose
from relay.selfimprove.archive import Archive, genome_id

# A defensive empty base used when the caller passes base_genome=None.
_EMPTY_BASE: dict = {"knobs": {}, "cards": {}, "parent_id": None, "note": "base"}


def _is_domain_general(genome: dict) -> bool:
    """True iff every card text in the genome passes overfit_lint (reuse propose.lint_candidate)."""
    return not _propose.lint_candidate(genome)


def diversify(base_genome, n, *, archive=None, rejected_ids=None) -> list[dict]:
    """Return up to N diverse genomes for an N-attempt best-of-N run.

    Guarantees (in order):
      - attempt 0 == base_genome (the strong incumbent), always present;
      - attempts 1..N-1 are DISTINCT domain-general mutations of base via propose.mutation_generator;
      - all returned genomes are domain-general (overfit_lint clean), have distinct genome_ids,
        exclude any id in `rejected_ids` (dead ideas) and any id already in `archive` (already tried);
      - never padded with duplicates -- fewer than N is returned if the generator runs dry.

    Edge cases:
      - base_genome is None        -> treated as the empty base {"knobs":{},"cards":{},...};
      - n <= 1                     -> [base] (single-shot, no diversification);
      - base itself fails the filters (rejected / archived / NOT domain-general) -> it is STILL the
        first attempt. The base is the trusted incumbent; the rejected/archive filters exist to keep
        NEW variants from re-treading dead/tried ideas, not to discard the incumbent. (mutations of a
        leaky base would inherit its leak and be dropped by the domain-general check anyway.)
    """
    base = base_genome if isinstance(base_genome, dict) else _EMPTY_BASE

    out: list[dict] = [base]
    seen_ids: set[str] = {genome_id(base)}

    try:
        n_int = int(n)
    except (TypeError, ValueError):
        n_int = 1
    if n_int <= 1:
        return out

    rejected = set(rejected_ids or ())

    # How many DISTINCT mutations do we still need? At most n_int - 1 (attempt 0 is base).
    needed = n_int - 1

    # mutation_generator is deterministic and indexes its knob/card library by i, cycling with a
    # period of len(_GENERIC_*). We ask for a generous batch so cycling doesn't starve us, then keep
    # only the distinct, domain-general, non-rejected, non-archived survivors. There is a finite pool
    # of distinct variants the placeholder generator can emit; once it cycles we stop adding.
    request = max(needed * 4, needed + len(_propose._GENERIC_CARDS) * len(_propose._GENERIC_KNOBS))
    raw = _propose.mutation_generator([], base, request) or []

    for cand in raw:
        if len(out) >= n_int:
            break
        if not isinstance(cand, dict):
            continue
        gid = genome_id(cand)
        if gid in seen_ids:                 # distinct genome_ids only (base + earlier survivors)
            continue
        if gid in rejected:                 # dead idea
            continue
        if archive is not None and archive.get(gid) is not None:  # already tried
            continue
        if not _is_domain_general(cand):    # must be domain-general
            continue
        stamped = dict(cand)
        stamped["parent_id"] = genome_id(base)
        out.append(stamped)
        seen_ids.add(gid)

    return out


def diversity_report(genomes) -> dict:
    """Quick check a caller (or test) can assert on.

    Returns {"n", "distinct_ids", "all_domain_general", "ids"} where:
      - n                  = number of genomes;
      - distinct_ids       = number of unique genome_ids (== n iff no duplicates);
      - all_domain_general = True iff every genome passes overfit_lint;
      - ids                = the genome_id of each genome, in order.
    """
    glist = list(genomes or [])
    ids = [genome_id(g) for g in glist]
    return {
        "n": len(glist),
        "distinct_ids": len(set(ids)),
        "all_domain_general": all(_is_domain_general(g) for g in glist),
        "ids": ids,
    }
