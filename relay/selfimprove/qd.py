"""Behaviour descriptors that describe behaviour.

WHAT WAS THERE, AND WHY IT DOES NOT FIT

`archive.descriptors` bins a genome by average diff size, average turn count and its most
common miss class. Those were written for the SWE loop, where a scaffold's character really
is visible in how many lines it touches, and its own docstring calls them a design bet.

Against CompanionBench they describe almost nothing. Every episode is a fixed task with a
fixed shape, so diff size is a property of the EPISODE rather than the harness, and two
genomes that behave completely differently -- one careful and slow, one that fabricates a
DONE -- land in the same cell. A quality-diversity map whose cells do not separate behaviours
is an expensive way to keep one elite.

WHAT REPLACES IT

Three axes, each computed from per-episode results the archive already carries, and each
chosen because a real harness varies along it:

  strength      which capability family it is best at. A harness good at spreadsheets and
                poor at long-running state is a different thing from its mirror image, and
                the map should hold both rather than ranking them.

  failure_mode  WHERE it fails when it fails: the task itself, the things around the task, or
                the security boundary. Two harnesses at the same pass rate with different
                failure modes are not interchangeable, and averaging them into one number is
                how that gets lost.

  caution       whether it over-claims. The product's defining failure is reporting DONE for
                work that never landed, so a harness that parks correctly when it should is
                occupying a genuinely different niche from one that guesses -- even at an
                identical score.

The point of a QD map is to keep behaviours a scalar would discard. These are the axes on
which this product's harnesses actually differ.
"""
from __future__ import annotations

from collections import Counter

#: Capability families, grouped from the episode categories. Coarser than the categories on
#: purpose: a map with one cell per category and a handful of genomes per campaign is a list
#: with extra steps.
FAMILIES = {
    "filesystem": "documents",
    "csv_json": "documents",
    "document": "documents",
    "excel": "documents",
    "ocr": "documents",
    "sql": "data",
    "routing": "orchestration",
    "long_running": "orchestration",
    "auth_consent": "orchestration",
    "security": "security",
    "steering": "instruction",
}

FAILURE_MODES = ("functional", "side_effect", "security", "infra", "none")
CAUTION = ("over_claims", "appropriately_cautious", "unknown")


def family_of(category: str) -> str:
    return FAMILIES.get(category or "", "other")


def descriptors(episode_results) -> dict:
    """Behaviour descriptors for one arm, from its per-episode results.

    Deliberately tolerant of missing fields: a result set from an older run should place the
    genome somewhere rather than crash the archive, and "unknown" is a legitimate cell.
    """
    rows = [r for r in (episode_results or []) if isinstance(r, dict)]
    if not rows:
        return {"strength": "unknown", "failure_mode": "none", "caution": "unknown"}

    return {
        "strength": _strength(rows),
        "failure_mode": _failure_mode(rows),
        "caution": _caution(rows),
    }


def _strength(rows) -> str:
    """The family with the highest success rate; ties broken alphabetically for determinism."""
    by_family = {}
    for r in rows:
        fam = family_of(r.get("category"))
        got, total = by_family.get(fam, (0, 0))
        by_family[fam] = (got + bool(r.get("success")), total + 1)
    scored = [(-(g / t), f) for f, (g, t) in by_family.items() if t]
    if not scored:
        return "unknown"
    best = min(scored)
    # A harness that passes nothing has no strength, and calling its least-bad family a
    # strength would put it in a cell it does not belong in.
    return "none" if best[0] == 0 else best[1]


def _failure_mode(rows) -> str:
    """Where the failures land. The FIRST applicable, not the most common.

    Ordered rather than counted because these are not comparable quantities: one security
    failure says more about a harness than five functional ones, and a count would let the
    five outvote it.
    """
    failed = [r for r in rows if not r.get("success")]
    if not failed:
        return "none"
    if any(r.get("security_score", 1.0) < 1.0 for r in failed):
        return "security"
    if any(r.get("infra_failure") for r in failed):
        return "infra"
    if any(r.get("side_effect_score", 1.0) < 1.0 for r in failed):
        return "side_effect"
    return "functional"


def _caution(rows) -> str:
    """Does it over-claim? Read from the episodes built to detect exactly that.

    The parked-job and consent episodes score a security failure specifically when a job that
    did not finish is reported as finished. That is the product's defining fault, so it earns
    its own axis rather than being folded into a rate.
    """
    relevant = [r for r in rows if r.get("category") in ("auth_consent", "long_running")]
    if not relevant:
        return "unknown"
    if any(r.get("security_score", 1.0) < 1.0 for r in relevant):
        return "over_claims"
    return "appropriately_cautious" if any(r.get("success") for r in relevant) else "unknown"


def cell_key(desc: dict) -> str:
    """Stable cell address. Same contract as archive.cell_key, different axes."""
    return "%s|%s|%s" % (desc.get("strength", "unknown"),
                         desc.get("failure_mode", "none"),
                         desc.get("caution", "unknown"))


def map_of(entries, metric="pass_at_1") -> dict:
    """{cell: best entry} over rows carrying semantic descriptors.

    Rows without them are SKIPPED rather than defaulted into a shared cell -- a default cell
    collects every legacy row into one bucket and then reports whichever happened to score
    highest as the elite of a behaviour nobody described.
    """
    elites = {}
    for entry in entries or []:
        desc = (entry.get("descriptors") or {}).get("semantic")
        if not desc:
            continue
        key = cell_key(desc)
        current = elites.get(key)
        if current is None or _metric(entry, metric) >= _metric(current, metric):
            elites[key] = entry
    return elites


def _metric(entry, metric):
    try:
        value = entry.get(metric)
        return float(value) if value is not None else float("-inf")
    except (TypeError, ValueError):
        return float("-inf")


def coverage(entries) -> dict:
    """How much of the behaviour space a campaign actually explored.

    The useful reading of a QD run is its SHAPE. A campaign that filled two cells out of many
    explored one behaviour and varied its dial, whatever its best score says, and that fact
    should be as visible as the score.
    """
    cells = map_of(entries)
    modes = Counter(cell_key(e["descriptors"]["semantic"]).split("|")[1]
                    for e in entries or []
                    if (e.get("descriptors") or {}).get("semantic"))
    return {
        "cells_occupied": len(cells),
        "failure_modes_seen": dict(modes),
        "described": sum(1 for e in entries or []
                         if (e.get("descriptors") or {}).get("semantic")),
        "total": len(entries or []),
    }
