# -*- coding: utf-8 -*-
"""Transfer test for the organisation genotype (slice E6): E5 candidates x other contexts.

WHAT DECISION THIS SERVES. Whether a genotype that beat its parent in the context it was found in
(one model, one task kind) is a real improvement or a local accident: does it still beat the parent
in OTHER contexts (other models, other task kinds)? The answer decides which Pareto-archive
entries are worth carrying forward as general changes and which are kept only as context-specific
ones. Per candidate and per context it measures the parent and the candidate with an evaluator
that is passed in (the same frozen-harness idea as genotype_bench), records the score delta, and
gives one verdict per candidate. It starts no job, submits nothing to the fleet, opens no network
connection and writes no file (nothing under .fleet). Nothing in production calls it (default
off): only scripts/org_genotype_transfer.py, run by hand, does, and that script writes only to the
--out path it is given.

CONTEXTS. A context is {"axis": "model" | "kind", "name": str, "home": bool}. The axis says what
varies (the model that runs the work, or the kind of task); a name is unique across all contexts
and may not be the sealed pool (the sealed pool is a milestone matter, genotype_bench, and is
refused here). "home" marks the context(s) the candidates were found in; at least one is required
and every home context must be won for the question to be asked at all. The evaluator is
evaluate(genes, context_name) -> {"score": number}, higher is better.

VERDICTS (applied in this order; every threshold is a parameter, none is hidden):
  insufficient  a home context has no usable measurement, or fewer than min_other_contexts other
                contexts do (the evaluator raised or returned no numeric score, for the parent or
                the candidate). An infrastructure failure is never scored as a loss.
  regresses     in any measured context, home included, the candidate is worse than the parent by
                more than `tolerance` (delta < -tolerance).
  insufficient  (second case, reason "no home gain") it does not beat the parent (delta >
                min_gain) in every home context, so there is nothing to transfer.
  transfers     it beats the parent in all home contexts and in at least
                ceil(other_share * number of measured other contexts) of the other contexts
                (at least one), and does not regress anywhere.
  home_only     it beats the parent at home, does not regress anywhere, but wins fewer other
                contexts than that.
min_gain, tolerance, other_share and min_other_contexts are explicit arguments with the module
defaults below; the report carries the values used ("rules").

PER-AXIS BREAKDOWN. For each axis ("model", "kind") and candidate the report gives how many
contexts were measured, won, regressed and the mean delta, so a candidate that transfers across
models but not across task kinds (or the reverse) is visible instead of averaged away.

HARD BUDGET. max_contexts and max_candidates are required and must be integers >= 1; there is no
default and no way to run unbounded. The number of contexts given may not exceed max_contexts (it
is refused, never silently cut: a dropped context would change what "transfers" means). More
candidates than max_candidates are cut deterministically (smallest genotype ids first) and the cut
is counted. The purpose of both is to bound evaluator cost before the loop starts: the parent is
measured once per context and each candidate once per context, so the evaluator is called at most
(max_candidates + 1) * max_contexts times (the evaluator is the expensive, fleet-backed part in
real use).

DETERMINISM. With the same inputs and a deterministic evaluator the report is identical:
candidates are ordered by genotype id and contexts keep the order given.

HOLDOUT CHECK BEFORE MEASURING. genotype_explore.holdout_rejections() is applied to the candidate
ids first. A candidate named in a leaked or over_budget finding (or every candidate, when the
holdout is unsealed, mismatched or overlaps dev) is excluded before any evaluator call, counted in
the report ("rejected_holdout", with the reason per id), and never gets a verdict.
"""
from __future__ import annotations

import json
import math

from relay.org import genotype as G
from relay.org import genotype_bench as B
from relay.org import genotype_explore as E
from relay.org import genotype_holdout as H

VERSION = 1
AXES = ("model", "kind")
VERDICTS = ("transfers", "home_only", "regresses", "insufficient")

# No measured noise level of the evaluator exists yet, so no non-zero value can be justified: the
# strict defaults (any gain counts, any loss counts) are the only ones that are not invented. A
# caller that has measured the evaluator's run-to-run spread passes it here.
DEFAULT_MIN_GAIN = 0.0
DEFAULT_TOLERANCE = 0.0
# "Most other contexts" is read as at least half, rounded up: a single other win out of three is
# not transfer, two out of three is.
DEFAULT_OTHER_SHARE = 0.5
# One other context is the least that can show anything beyond the home context.
DEFAULT_MIN_OTHER_CONTEXTS = 1


class TransferError(ValueError):
    """The request cannot be run as asked (missing budget, bad contexts, sealed pool, bad input)."""


def _budget(name, value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TransferError("%s must be an integer >= 1" % name)
    return value


def dry_run_evaluator(salt=0):
    """A deterministic stand-in evaluator for dry runs: score in [0, 1) from genes and context.

    It measures nothing real; it exists so the test can be exercised end to end without a fleet.
    Its reports are synthetic and must not be read as benchmark results."""
    return E.dry_run_evaluator(salt)


def parse_context(text):
    """'axis:name' or 'axis:name:home' -> a context dict (the command-line spelling)."""
    parts = str(text).split(":")
    if len(parts) not in (2, 3) or (len(parts) == 3 and parts[2] != "home"):
        raise TransferError("context must be AXIS:NAME or AXIS:NAME:home, got %r" % (text,))
    return {"axis": parts[0], "name": parts[1], "home": len(parts) == 3}


def _contexts(contexts, max_contexts):
    contexts = [dict(c) for c in contexts]
    if not contexts:
        raise TransferError("no contexts given")
    if len(contexts) > max_contexts:
        raise TransferError("%d contexts given but max_contexts is %d" % (len(contexts),
                                                                          max_contexts))
    names = set()
    for c in contexts:
        if c.get("axis") not in AXES:
            raise TransferError("context axis must be one of %s, got %r" % (AXES, c.get("axis")))
        name = c.get("name")
        if not isinstance(name, str) or not name:
            raise TransferError("context name must be a non-empty string")
        if name.lower() == B.SEALED:
            raise TransferError("the transfer test never reads the sealed pool")
        if name in names:
            raise TransferError("context %r is named twice" % name)
        names.add(name)
        c["home"] = bool(c.get("home", False))
    if not any(c["home"] for c in contexts):
        raise TransferError("at least one context must be marked home")
    return contexts


def _candidates(parent_genes, candidates):
    """[(id, genes)] sorted by id, parent and repeats dropped; also the count of dropped ones."""
    root = G.genotype_hash(parent_genes, parent_genes)
    found, dropped = {}, 0
    for c in candidates:
        if isinstance(c, dict) and isinstance(c.get("genotype"), dict):
            genes = dict(c["genotype"]["genes"])
            gid = c["genotype"].get("id") or G.genotype_hash(genes, parent_genes)
        else:
            genes = dict(c)
            gid = G.genotype_hash(genes, parent_genes)
        G.validate(genes)
        if gid == root or gid in found:
            dropped += 1
            continue
        found[gid] = genes
    return sorted(found.items()), dropped


def _measure(evaluate, genes, context):
    """The score for one run, or None when the evaluator failed (infrastructure, not a loss)."""
    try:
        s = B._score(evaluate(dict(genes), context))
    except Exception:  # noqa: BLE001 - any harness failure is an infra failure, not a loss
        return None
    return s


def verdict_for(deltas, contexts, min_gain=DEFAULT_MIN_GAIN, tolerance=DEFAULT_TOLERANCE,
                other_share=DEFAULT_OTHER_SHARE, min_other_contexts=DEFAULT_MIN_OTHER_CONTEXTS):
    """(verdict, reason) for one candidate from {context name: delta or None} and the contexts.

    The rules are the ones in the module docstring; this function is the only place they live."""
    home = [c["name"] for c in contexts if c["home"]]
    other = [c["name"] for c in contexts if not c["home"]]
    if any(deltas.get(n) is None for n in home):
        return "insufficient", "home context not measured"
    other_ok = [n for n in other if deltas.get(n) is not None]
    if len(other_ok) < min_other_contexts:
        return "insufficient", "fewer than %d other contexts measured" % min_other_contexts
    measured = [n for n in home + other_ok]
    if any(deltas[n] < -tolerance for n in measured):
        return "regresses", "worse than parent beyond tolerance in " + ", ".join(
            n for n in measured if deltas[n] < -tolerance)
    if any(deltas[n] <= min_gain for n in home):
        return "insufficient", "no home gain"
    need = max(1, int(math.ceil(other_share * len(other_ok))))
    wins = sum(1 for n in other_ok if deltas[n] > min_gain)
    if wins >= need:
        return "transfers", "beats parent at home and in %d of %d other contexts" % (
            wins, len(other_ok))
    return "home_only", "beats parent at home but in %d of %d other contexts (need %d)" % (
        wins, len(other_ok), need)


def _axis_breakdown(deltas, contexts, min_gain, tolerance):
    out = {}
    for axis in AXES:
        names = [c["name"] for c in contexts if c["axis"] == axis]
        ok = [deltas[n] for n in names if deltas.get(n) is not None]
        out[axis] = {"contexts": len(names), "measured": len(ok),
                     "wins": sum(1 for d in ok if d > min_gain),
                     "regressions": sum(1 for d in ok if d < -tolerance),
                     "mean_delta": (sum(ok) / len(ok)) if ok else None}
    return out


def transfer(parent_genes, candidates, contexts, evaluate, max_contexts, max_candidates,
             min_gain=DEFAULT_MIN_GAIN, tolerance=DEFAULT_TOLERANCE,
             other_share=DEFAULT_OTHER_SHARE, min_other_contexts=DEFAULT_MIN_OTHER_CONTEXTS,
             sealed=None, dev_ids=None, holdout_ids=None, exposures=None,
             budget=H.DEFAULT_BUDGET, harness_id=""):
    """Run the transfer test and return a report dict (nothing is written anywhere).

    `candidates` is a list of genes dicts or of genotype_candidates records ({"genotype": ...}).
    Report: {"version", "harness", "budget": {"max_contexts", "max_candidates"}, "rules",
    "parent", "contexts", "evaluated", "rejected_holdout": {id: reason}, "rejected_count",
    "dropped", "cut", "results": [{"id", "deltas": {context: delta or None}, "verdict",
    "reason", "axes"}], "counts": {verdict: n}}."""
    max_contexts = _budget("max_contexts", max_contexts)
    max_candidates = _budget("max_candidates", max_candidates)
    if not 0 < other_share <= 1:
        raise TransferError("other_share must be in (0, 1]")
    if tolerance < 0 or min_gain < 0:
        raise TransferError("min_gain and tolerance must be >= 0")
    if isinstance(min_other_contexts, bool) or not isinstance(min_other_contexts, int) \
            or min_other_contexts < 1:
        raise TransferError("min_other_contexts must be an integer >= 1")
    contexts = _contexts(contexts, max_contexts)
    parent_genes = dict(parent_genes)
    G.validate(parent_genes)
    root = G.genotype_hash(parent_genes, parent_genes)
    pairs, dropped = _candidates(parent_genes, candidates)
    rejected, _ = E.holdout_rejections([i for i, _ in pairs], sealed, dev_ids, holdout_ids,
                                       exposures, budget=budget)
    pairs = [(i, g) for i, g in pairs if i not in rejected]
    cut = max(0, len(pairs) - max_candidates)
    pairs = pairs[:max_candidates]
    names = [c["name"] for c in contexts]
    evaluated = 0
    parent_scores = {}
    if pairs:  # with nothing to compare, the parent is not measured either
        for n in names:
            parent_scores[n] = _measure(evaluate, parent_genes, n)
            evaluated += 1
    results, counts = [], {v: 0 for v in VERDICTS}
    for gid, genes in pairs:
        deltas = {}
        for n in names:
            s = _measure(evaluate, genes, n)
            evaluated += 1
            p = parent_scores[n]
            deltas[n] = None if s is None or p is None else s - p
        v, why = verdict_for(deltas, contexts, min_gain, tolerance, other_share,
                             min_other_contexts)
        counts[v] += 1
        results.append({"id": gid, "deltas": deltas, "verdict": v, "reason": why,
                        "axes": _axis_breakdown(deltas, contexts, min_gain, tolerance)})
    return {"version": VERSION, "harness": str(harness_id),
            "budget": {"max_contexts": max_contexts, "max_candidates": max_candidates},
            "rules": {"min_gain": min_gain, "tolerance": tolerance, "other_share": other_share,
                      "min_other_contexts": min_other_contexts},
            "parent": root, "contexts": contexts, "evaluated": evaluated,
            "rejected_holdout": rejected, "rejected_count": len(rejected),
            "dropped": dropped, "cut": cut, "results": results, "counts": counts}


def to_json(report):
    return json.dumps(report, sort_keys=True, indent=2)
