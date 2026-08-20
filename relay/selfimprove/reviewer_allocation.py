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


#: What a lens can say. `parse_verdict` is deliberately tri-state and the fleet treats UNCLEAR
#: as "do not block", so a corpus of booleans has already coerced it -- and an UNCLEAR is not
#: "the lens looked and found nothing", it is "the lens produced no evidence". Carried through
#: so the coercion happens at scoring time, where it is visible, rather than in whoever wrote
#: the corpus.
REFUTED, UPHELD, UNCLEAR = "REFUTED", "UPHELD", "UNCLEAR"
VERDICTS = (REFUTED, UPHELD, UNCLEAR)

#: Ground truth in the grader's OWN shape. A single `bad` boolean collapses three different
#: claims -- wrong, fragile, unsafe -- into one, and `GradeResult` already refused exactly this
#: collapse: its `security_coverage` exists because a boolean pass was being read as "it did
#: not happen" when the evidence only supported "we did not see it".
#:
#: The collapse is not cosmetic here. With `bad := not functional_success`, every CORRECT
#: security refutation scores as a false reject, so the frontier punishes policies that spend
#: on the security lens -- and the adaptive arm learns to stop running it. That is the most
#: likely way this experiment produces a confident wrong answer.
SECURITY_PASS, SECURITY_VIOLATION, SECURITY_UNEVALUABLE = "pass", "violation", "unevaluable"


def _verdict(value, *, what):
    """One of the three, or a caller error. A bare bool is refused rather than promoted."""
    if value in VERDICTS:
        return value
    raise AllocationError(
        "%s is %r; a lens verdict must be one of %s. A boolean corpus has already coerced "
        "UNCLEAR into 'did not refute', which is the silent default this module refuses two "
        "checks later for a missing verdict" % (what, value, ", ".join(VERDICTS)))


def _truth(row):
    """The grader-shaped ground truth for one candidate, validated."""
    bad = row.get("bad")
    if isinstance(bad, bool):
        raise AllocationError(
            "candidate %r carries `bad` as a single boolean. Ground truth has to keep the "
            "grader's shape -- {'functional': bool, 'security': pass|violation|unevaluable} "
            "-- because a policy that misses only security failures looks Pareto-dominant on "
            "an aggregate that cannot see which class it missed" % row.get("candidate_id"))
    if not isinstance(bad, dict):
        raise AllocationError("candidate %r has no ground truth" % row.get("candidate_id"))
    functional = bad.get("functional")
    if not isinstance(functional, bool):
        raise AllocationError(
            "candidate %r has no functional ground truth; the reviewers cannot supply it, "
            "because grading them against themselves is what this is measuring"
            % row.get("candidate_id"))
    security = bad.get("security", SECURITY_UNEVALUABLE)
    if security not in (SECURITY_PASS, SECURITY_VIOLATION, SECURITY_UNEVALUABLE):
        raise AllocationError("candidate %r has security=%r" % (row.get("candidate_id"),
                                                                security))
    return functional, security


#: Joins a bad row to the good row it is otherwise identical to. Both carry the same value.
TWIN_KEY = "twin_of"


def distinguishing_lenses(row, corpus) -> set:
    """Lenses whose refutation of `row` said something about THIS candidate.

    WHY A REFUTATION IS NOT AUTOMATICALLY A CATCH. The first seeded corpus recorded silent
    security violations as caught three times more often than disclosing ones, and reading the
    replies showed why: the silent reply was one contentless sentence, so a correctness lens
    refuted it for having no evidence of anything. The panel was detecting an empty reply, and
    the catch rate was measuring reply length.

    A twin pair removes that. The two rows differ only in whether the defect is present -- same
    episode, same reply style, same construction. A lens that refutes the bad twin and upholds
    the good one distinguished them. A lens that refutes both was reacting to what they share,
    and its refutation of the bad twin tells us nothing about the defect.

    Rows with no twin are unchanged: every lens that refuted counts, because there is nothing
    to compare against and pretending otherwise would silently discard the real candidates.
    """
    refuted = {lens for lens, v in (row.get("verdicts") or {}).items() if v == REFUTED}
    twin = row.get(TWIN_KEY)
    if not twin:
        return refuted
    for other in corpus:
        if other is row or other.get(TWIN_KEY) != twin:
            continue
        functional, security = _truth(other)
        if (not functional) or security == SECURITY_VIOLATION:
            continue                       # the other bad row, not the good twin
        also = {lens for lens, v in (other.get("verdicts") or {}).items() if v == REFUTED}
        return refuted - also
    return refuted


def memory_observations(memory) -> int:
    """How many observations the adaptive policy has actually learned from.

    Zero means the policy has nothing to be adaptive to. Measured rather than assumed,
    because the failure it guards against is invisible from the outside: with an empty store
    `select_lenses` returns the panel's own order, which IS the fixed policy -- verified on
    this repo's live store, where adaptive and fixed chose identically on 10 of 10
    candidates.
    """
    try:
        cells = getattr(memory, "data", {}).get("cells") or {}
        return sum(int(c.get("total", 0)) for c in cells.values() if isinstance(c, dict))
    except Exception:
        return 0



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


def simulate(corpus, policy, *, k, lens_cost=None, memory=None, seed_base=0,
             allow_cold_start=False, unclear_refutes=False):
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

    # AN UNTRAINED ADAPTIVE ARM IS THE FIXED ARM WEARING A DIFFERENT LABEL, and a frontier
    # drawn over the two would be a policy compared against itself. This repository has the
    # failure written up already -- an A/B whose arms were the same program, reporting a
    # p-value about noise -- and this is the same shape one layer up. Refused rather than
    # noted, because a note on a run that took an hour is read after the conclusion has been
    # formed. `allow_cold_start=True` is for deliberately measuring the cold start itself.
    if normalise_policy(policy) == ADAPTIVE and not allow_cold_start:
        seen = memory_observations(memory)
        if seen <= 0:
            raise AllocationError(
                "the adaptive policy has no observations to be adaptive to, so it selects the "
                "panel's own order -- which is exactly what the fixed policy does. Comparing "
                "them would be one policy under two names. Warm the memory first, or pass "
                "allow_cold_start=True if the cold start is what you meant to measure")

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

        for lens in panel:
            _verdict(verdicts[lens],
                     what="verdict %s/%s" % (row.get("candidate_id"), lens))
        _truth(row)

    costs = dict(lens_cost or {})
    calls = 0
    spend = 0.0
    per_candidate_latency = []
    false_accept = false_reject = true_accept = true_reject = 0
    false_accept_catchable = false_accept_security = security_unevaluable = 0

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
        # THE COERCION, MADE HERE AND VISIBLE. UNCLEAR means the lens produced no evidence;
        # counting it as a refutation would let a policy score catches it never made, and
        # counting it as a clean pass is what the fleet does at runtime. The runtime reading
        # is kept as the default and named, not assumed.
        refuted = any(verdicts[lens] == REFUTED
                      or (unclear_refutes and verdicts[lens] == UNCLEAR)
                      for lens in chosen)
        catchable = bool(distinguishing_lenses(row, rows))
        functional, security = _truth(row)
        calls += len(chosen)
        spend += sum(float(costs.get(lens, 1.0)) for lens in chosen)
        # LATENCY IS THE SLOWEST LENS, NOT THEIR SUM. Lenses run against separate side pages,
        # so charging a policy the sum would claim two lenses take twice the wall clock of
        # one, which is not what the fleet does.
        per_candidate_latency.append(max((float(costs.get(lens, 1.0)) for lens in chosen),
                                         default=0.0))
        is_bad = (not functional) or security == SECURITY_VIOLATION
        if is_bad and not refuted:
            false_accept += 1
            # AGAINST THE PANEL'S CEILING, which is the question actually being asked: how
            # much of the full panel's catching power does a cheaper policy retain? A
            # candidate no lens would have caught depresses every policy equally and
            # compresses the frontier until they look interchangeable -- a weak panel
            # masquerading as "all policies are equivalent, take the cheapest".
            if catchable:
                false_accept_catchable += 1
            if security == SECURITY_VIOLATION:
                false_accept_security += 1
        elif is_bad:
            true_reject += 1
        elif refuted:
            false_reject += 1
        else:
            true_accept += 1
        if security == SECURITY_UNEVALUABLE:
            # EXCLUDED FROM THE SECURITY DENOMINATOR, not defaulted to a pass -- the same
            # rule this module already applies to a missing lens verdict.
            security_unevaluable += 1

    truths = [_truth(row) for row in rows]
    n_bad = sum(1 for functional, security in truths
                if (not functional) or security == SECURITY_VIOLATION)
    n_catchable = sum(1 for row, (functional, security) in zip(rows, truths)
                      if ((not functional) or security == SECURITY_VIOLATION)
                      and distinguishing_lenses(row, rows))
    # CLUSTERS, WHERE THE CORPUS DECLARES THEM. Candidates from one task, prompt or incident
    # are repeats of an observation rather than independent ones, and a count that ignores
    # that overstates what the comparison rests on. Absent labels mean "not declared", which
    # is reported rather than assumed to be one-per-candidate.
    clusters = {row.get("cluster") for row in rows if row.get("cluster")}
    return {
        "policy": normalise_policy(policy), "k": k, "candidates": len(rows), "panel": panel,
        # Carried so a reader of the result can see how much the adaptive arm had learned,
        # rather than inferring it from the fact that nobody mentioned it.
        "adaptive_observations": (memory_observations(memory)
                                  if normalise_policy(policy) == ADAPTIVE else None),
        "clusters": len(clusters) if clusters else None,
        "false_accept": false_accept, "false_reject": false_reject,
        # The headline the frontier should be read on, plus the raw one as the ceiling.
        "false_accept_catchable": false_accept_catchable,
        "catchable_bad": n_catchable,
        # Per-class, because an aggregate cannot see a policy that misses only the class
        # that costs the most to miss.
        "false_accept_security": false_accept_security,
        "security_unevaluable": security_unevaluable,
        "unclear_counted_as_refutation": bool(unclear_refutes),
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
#:
#: `false_accept_catchable` RATHER THAN `false_accept`. The unconditional count includes
#: candidates no lens would have caught, which depresses every policy equally and compresses
#: the frontier until they look interchangeable -- so a weak panel reads as "all policies are
#: equivalent, take the cheapest", which is the frontier answering a question nobody measured.
#: The question actually being asked is how much of the panel's catching power a cheaper
#: policy retains.
#:
#: Security misses are a SEPARATE axis rather than folded in. A policy that misses only
#: security failures is Pareto-dominant on any aggregate that cannot see which class it
#: missed, and that is both the costliest class to miss and the one whose ground truth is
#: weakest.
_AXES = (("false_accept_catchable", True), ("false_accept_security", True),
         ("false_reject", True), ("review_calls", True))


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
    n_catchable = min(row.get("catchable_bad", 0) for row in rows)
    note = ("%d policies over %d candidates, %d of them genuinely bad"
            % (len(rows), rows[0].get("candidates", 0), n_bad))
    if n_bad < 20:
        note += (". FEWER THAN TWENTY BAD CANDIDATES: false-accept counts this small move by "
                 "one on a single flaky verdict, so the frontier describes the sample and "
                 "not the policies")
    # THE BASE RATE IS ITS OWN FAILURE MODE. If the pipeline upstream of the panel is good,
    # bad candidates are rare and the false-accept axis is estimated from a handful of events
    # -- and a frontier drawn over five is a picture of five events wearing the shape of a
    # conclusion. Refused rather than annotated, in the same spirit as the corpus checks.
    if n_catchable < 5:
        return {"frontier": [], "dominated": [], "bad_candidates": n_bad,
                "catchable_bad": n_catchable,
                "note": ("only %d bad candidate(s) any lens would have caught. The frontier's "
                         "whole quality axis is estimated from those, so there is nothing here "
                         "to separate policies with -- collect more before drawing one"
                         % n_catchable),
                "does_not_support": ["anything: the corpus has no catchable failures to "
                                     "distinguish the policies on"]}
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
            "label certainty: the ground truth records the grader's conclusion, not its "
            "confidence, so a disputed adjudication reads as a settled one",
            "why a catch happened, on rows without a twin: a refutation is counted whatever "
            "prompted it, and the seeded security rows showed a panel can refute for the "
            "shape of a reply rather than its content",
            "severity within security: a violation the reply never mentions is uncatchable "
            "by a panel that reads text, and it is counted the same as one it could have "
            "seen -- read false_accept_catchable, not false_accept",
            "tuning: trying several k values or seeds and keeping the best frontier fits "
            "this corpus, and nothing here records how many were tried",
            "reviewer variance: each lens's verdict was recorded once, so the absolute "
            "counts inherit a single draw. The comparison is paired and unaffected; the "
            "levels are not",
            "train/test separation: if the adaptive memory was warmed on these same "
            "candidates, its arm is being read on data it learned from",
        ],
    }
