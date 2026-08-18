"""Section 18: how many reviewers to spend, and what that buys.

THE EXISTING BEHAVIOUR THIS TURNS INTO AN EXPERIMENT

`refuter_memory` already learns per-lens rejection rates and, behind
`MCP_ADAPTIVE_REFUTER=1`, runs only the top-k lenses most likely to refute this candidate.
That is a policy. It has never been compared against the alternatives, so "adaptive" is a
name rather than a finding.

The brief asks for four policies -- all, fixed, random, adaptive -- scored on false accept,
false reject, review calls, latency and cost, and read as a Pareto frontier between
verification quality and review cost.

THE THING THAT MAKES THIS MEASURABLE AT ALL

A policy that runs two of five lenses cannot be scored from its own run. Whether it FALSELY
ACCEPTED depends on what the three lenses it skipped would have said, and that is exactly
what a subset run does not know. So the evaluation needs a corpus where EVERY lens was run
against every candidate, and the policies are then simulated over it: each policy picks its
subset, and the subset's verdicts are looked up rather than re-derived.

This is not a detail. Scoring a subset policy against only the lenses it chose measures
"did the lenses it ran agree with each other", which is close to zero information and looks
like a result. `simulate` therefore REFUSES a corpus with missing cells rather than treating
absence as "no refutation" -- the direction that would silently flatter every cheap policy.

WHAT IT DOES NOT DO

It does not declare a winner. Four policies over a corpus this size produce a frontier of
three or four points with overlapping intervals, and a Pareto frontier is a picture of the
trade-off rather than a decision about it. `frontier` returns the non-dominated set and the
counts it rests on; choosing among them is a judgement about how much a missed defect costs
relative to a review call, which is not a fact about the data.
"""
from __future__ import annotations

import hashlib
import random

#: The four allocation policies the brief names.
ALL = "all"
FIXED = "fixed"
RANDOM = "random"
ADAPTIVE = "adaptive"
POLICIES = (ALL, FIXED, RANDOM, ADAPTIVE)


class AllocationError(ValueError):
    """Raised when a corpus cannot support the comparison being asked of it."""


def normalise_policy(policy) -> str:
    """One spelling per policy. `choose` lowercased a local copy while `simulate` returned
    what it was given, so "ALL" and "all" could appear as two separate points on one
    frontier -- a comparison of a policy against itself, wearing two labels."""
    return str(policy or "").strip().lower()


def _strict_bool(value, *, what):
    """True/False only. `bool("false")` is True, and at this seam that turns a lens that did
    NOT refute into one that did -- or a good candidate into a bad one."""
    if not isinstance(value, bool):
        raise AllocationError("%s must be a bool, got %r" % (what, type(value).__name__))
    return value


def _candidate_seed(seed_base, candidate_id):
    """A draw tied to the CANDIDATE, not to its position in the list.

    Seeding from the row index made the random arm reproducible only for the same corpus in
    the same order and the same length -- so adding one candidate reshuffled every later
    draw, and the arm silently became a different experiment. Hashing the id keeps each
    candidate's allocation stable no matter what else is in the corpus.
    """
    digest = hashlib.sha256(("%s|%s" % (seed_base, candidate_id)).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _validate_selection(chosen, panel, k, policy):
    """A policy's answer has to be a subset of the panel, without repeats, within budget.

    The adaptive policy is an object supplied by the caller. Left unchecked it can return a
    lens that is not in the panel (a KeyError deep in the scoring loop), the same lens twice
    (inflating calls and cost), or nothing at all -- and an empty selection ACCEPTS every
    candidate at zero cost, which is the cheapest possible policy and the most wrong.
    """
    out = list(chosen)
    if not out:
        raise AllocationError(
            "%s selected no lens; an empty panel accepts everything at zero cost, which "
            "would score as the cheapest policy on the frontier" % policy)
    if len(set(out)) != len(out):
        raise AllocationError("%s selected a lens twice: %s" % (policy, out))
    unknown = [lens for lens in out if lens not in panel]
    if unknown:
        raise AllocationError("%s selected %s, which is not in the panel" % (policy, unknown))
    if policy != ALL and len(out) > k:
        raise AllocationError("%s returned %d lenses for a budget of %d"
                              % (policy, len(out), k))
    return out


def choose(policy, lenses, *, k, features=None, memory=None, seed=None):
    """Which lenses this policy would run for one candidate.

    `lenses` is the full panel in its declared order. `k` is the budget in lens-calls.

    RANDOM TAKES A SEED AND WILL NOT INVENT ONE. An unseeded random arm makes the experiment
    unreproducible, and an experiment nobody can re-run is an anecdote with a p-value. The
    seed is per-candidate so the draw differs between candidates while the whole run replays
    identically.
    """
    panel = list(lenses)
    if not panel:
        return []
    policy = normalise_policy(policy)
    k = max(1, int(k))

    if policy == ALL:
        return panel
    if policy == FIXED:
        # The first k in the panel's own order: a fixed panel, chosen once by a human and
        # never revisited. This is the honest baseline for "we did not think about it".
        return panel[:k]
    if policy == RANDOM:
        if seed is None:
            raise AllocationError(
                "the random policy needs an explicit seed; an unseeded arm cannot be "
                "replayed, and a comparison that cannot be replayed is not evidence")
        return random.Random(seed).sample(panel, min(k, len(panel)))
    if policy == ADAPTIVE:
        if memory is None:
            raise AllocationError(
                "the adaptive policy needs the learned memory it is adaptive to; without it "
                "it is the fixed policy wearing a different label")
        return list(memory.select_lenses(features or {}, panel, k))
    raise AllocationError("%r is not one of %s" % (policy, ", ".join(POLICIES)))


def simulate(corpus, policy, *, k, lens_cost=None, memory=None, seed_base=0):
    """Score one policy over a corpus where every lens was run against every candidate.

    Each corpus row is
        {"candidate_id", "bad": bool, "verdicts": {lens: refuted_bool}, "features": {...}}

    `bad` is the ground truth -- whether the candidate SHOULD have been rejected -- and it
    has to come from somewhere outside the reviewers, or the experiment is asking the
    reviewers to grade themselves.

    Returns counts, never rates alone: a rate without its denominator cannot be pooled,
    compared, or given an interval later.
    """
    rows = list(corpus or [])
    if not rows:
        raise AllocationError("an empty corpus cannot compare anything")

    panel = sorted({lens for row in rows for lens in (row.get("verdicts") or {})})
    if not panel:
        raise AllocationError("no lens verdicts in the corpus")

    seen_ids = set()
    for row in rows:
        cid = row.get("candidate_id")
        if not cid:
            raise AllocationError(
                "every candidate needs an id: without one the random arm cannot draw the "
                "same allocation twice, and a duplicated row cannot be spotted")
        if cid in seen_ids:
            raise AllocationError(
                "candidate %r appears twice; a repeat is not a second observation and "
                "counting it as one narrows every interval computed later" % cid)
        seen_ids.add(cid)
        verdicts = row.get("verdicts") or {}
        missing = [lens for lens in panel if lens not in verdicts]
        if missing:
            # REFUSED, NOT DEFAULTED. Treating a missing verdict as "did not refute" makes
            # every cheap policy look better exactly where it was not measured.
            raise AllocationError(
                "candidate %r has no verdict for %s. A subset policy cannot be scored "
                "without knowing what the lenses it skipped would have said, so the corpus "
                "must be a full all-lenses run"
                % (row.get("candidate_id"), ", ".join(missing)))
        if not isinstance(row.get("bad"), bool):
            raise AllocationError(
                "candidate %r has no ground truth; the reviewers cannot supply it, because "
                "grading them against themselves is what this is measuring"
                % row.get("candidate_id"))
        for lens in panel:
            _strict_bool(verdicts[lens],
                         what="verdict %s/%s" % (row.get("candidate_id"), lens))

    costs = dict(lens_cost or {})
    calls = 0
    spend = 0.0
    per_candidate_latency = []
    false_accept = false_reject = true_accept = true_reject = 0

    for i, row in enumerate(rows):
        # ONE CALL PER CANDIDATE, and that is load-bearing rather than tidy. The adaptive
        # policy's `select_lenses` advances a persistent exploration counter and writes it to
        # disk, so asking it twice for the same candidate -- once for the verdict and once to
        # price the latency -- would double the exploration period and change the very
        # behaviour being measured. Everything this candidate needs comes out of this one
        # selection.
        chosen = _validate_selection(
            choose(policy, panel, k=k, features=row.get("features"), memory=memory,
                   seed=_candidate_seed(seed_base, row["candidate_id"])),
            panel, k, normalise_policy(policy))
        verdicts = row["verdicts"]
        refuted = any(verdicts[lens] for lens in chosen)
        calls += len(chosen)
        spend += sum(float(costs.get(lens, 1.0)) for lens in chosen)
        # LATENCY IS THE SLOWEST LENS, NOT THEIR SUM. Lenses run against separate side pages,
        # so charging a policy the sum would claim two lenses take twice the wall clock of
        # one, which is not what the fleet does.
        per_candidate_latency.append(max((float(costs.get(lens, 1.0)) for lens in chosen),
                                         default=0.0))
        if row["bad"] and not refuted:
            false_accept += 1
        elif row["bad"]:
            true_reject += 1
        elif refuted:
            false_reject += 1
        else:
            true_accept += 1

    n_bad = sum(1 for row in rows if row["bad"])
    # CLUSTERS, WHERE THE CORPUS DECLARES THEM. Candidates from one task, prompt or incident
    # are repeats of an observation rather than independent ones, and a count that ignores
    # that overstates what the comparison rests on. Absent labels mean "not declared", which
    # is reported rather than assumed to be one-per-candidate.
    clusters = {row.get("cluster") for row in rows if row.get("cluster")}
    return {
        "policy": normalise_policy(policy), "k": k, "candidates": len(rows), "panel": panel,
        "clusters": len(clusters) if clusters else None,
        "false_accept": false_accept, "false_reject": false_reject,
        "true_accept": true_accept, "true_reject": true_reject,
        "bad_candidates": n_bad, "good_candidates": len(rows) - n_bad,
        "review_calls": calls,
        "calls_per_candidate": round(calls / len(rows), 3),
        "cost": round(spend, 3),
        "latency_total": round(sum(per_candidate_latency), 3),
        "latency_per_candidate": round(
            sum(per_candidate_latency) / len(rows), 3) if rows else 0.0,
    }


#: Axes for the frontier: (key, lower_is_better).
_AXES = (("false_accept", True), ("false_reject", True), ("review_calls", True))


def dominates(a, b) -> bool:
    """Whether `a` is at least as good as `b` everywhere and strictly better somewhere."""
    at_least = all((a[key] <= b[key]) if lower else (a[key] >= b[key])
                   for key, lower in _AXES)
    strictly = any((a[key] < b[key]) if lower else (a[key] > b[key])
                   for key, lower in _AXES)
    return at_least and strictly


def frontier(results) -> dict:
    """The non-dominated policies, and how much the comparison actually rests on.

    NO WINNER IS NAMED. Choosing a point on a frontier is a statement about how much a missed
    defect costs relative to a review call, and that is a judgement rather than a measurement.
    What is returned is the set nothing beats, plus the counts -- so a reader can see that a
    frontier drawn over a handful of bad candidates is a picture and not a conclusion.
    """
    rows = list(results or [])
    if not rows:
        return {"frontier": [], "dominated": [], "note": "nothing to compare"}
    keep, drop = [], []
    for row in rows:
        if any(dominates(other, row) for other in rows if other is not row):
            drop.append(row["policy"])
        else:
            keep.append(row["policy"])
    n_bad = min(row.get("bad_candidates", 0) for row in rows)
    note = ("%d policies over %d candidates, %d of them genuinely bad"
            % (len(rows), rows[0].get("candidates", 0), n_bad))
    if n_bad < 20:
        note += (". FEWER THAN TWENTY BAD CANDIDATES: false-accept counts this small move by "
                 "one on a single flaky verdict, so the frontier describes the sample and "
                 "not the policies")
    clusters = [row.get("clusters") for row in rows if row.get("clusters")]
    if clusters and min(clusters) < len(rows[0].get("panel") or []) * 4:
        note += (". Only %d declared clusters: candidates sharing a task or prompt are "
                 "repeats, so the effective sample is smaller than the candidate count"
                 % min(clusters))
    return {
        "frontier": sorted(keep), "dominated": sorted(drop), "note": note,
        "bad_candidates": n_bad,
        # SAID HERE RATHER THAN LEFT TO BE DISCOVERED. Each of these is a claim the output
        # cannot support, and each is one a reader could reasonably think it does.
        "does_not_support": [
            "generalisation: these are realised counts on one corpus, not rates with "
            "intervals, and nothing here is held out",
            "severity: every false accept counts one, so a missed cosmetic nit and a missed "
            "security defect are the same number",
            "label certainty: `bad` is a boolean, so a disputed adjudication reads as a "
            "settled one",
            "tuning: trying several k values or seeds and keeping the best frontier fits "
            "this corpus, and nothing here records how many were tried",
        ],
    }
