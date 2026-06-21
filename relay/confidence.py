"""PER-TASK CONFIDENCE + ABSTENTION -- calibrated humility over a best-of-N selection.

This is the testable core of Bet #2 in bench/AGENT_STRENGTHS.md (a self-knowing, calibrated agent),
built directly on Bet #1's SELECTOR (relay/bestofn.py). A single-attempt, per-token CLI structurally
cannot have this: with no N attempts there is no consensus to measure, no margin between a winner and a
runner-up, and so no principled way to ABSTAIN ("don't silently ship -- escalate"). Best-of-N gives us
exactly those signals, and this module turns them into a single calibrated confidence in [0,1] plus a
binary abstain/escalate decision a router can act on.

Pure, deterministic, signal-only: it imports relay.bestofn READ-ONLY and operates solely on the
candidate signals each attempt already carries. No fleet, no network, no subprocess, no real diffing.

Candidate shape is exactly relay/bestofn.py's:
    {"idx", "diff", "selftest_passed", "refuter_refuted", "refuter_total", "diff_size"}

The four confidence factors (the blend is documented at task_confidence; weights live in CONF_WEIGHTS,
a single tunable surface == a future self-improvement genome knob):

  1. selftest  -- the winner's own red->green self-test: True is a strong vote FOR confidence, None is
                  neutral, False is a strong vote AGAINST. (Mirrors bestofn's selftest dominance.)
  2. consensus -- the fraction of the fleet that converged on the winner's normalized diff
                  (cluster_size / N). Broad convergence => more confident; a lone winner => less.
  3. refuter   -- survival fraction (total - refuted)/total of the winner; 0.5 (neutral) if none ran.
                  Independent adversaries that could NOT break the patch raise confidence.
  4. margin    -- the winner's score minus the runner-up's, squashed to [0,1]. A clear lead means the N
                  did not merely diverge into ties => more confident; a near-tie means the fleet could
                  not agree which attempt is best => low confidence even if other signals look ok.

Each factor is mapped to [0,1] (1.0 = maximally confidence-raising). The factors are blended by a
weighted average using CONF_WEIGHTS, then clamped to [0,1]. Empty-diff / no-real-candidate winners are
forced to very low confidence regardless of the blend.
"""
from __future__ import annotations

from typing import Iterable

from relay import bestofn


# --------------------------------------------------------------------------------------------------
# CONF_WEIGHTS -- the single tunable surface (a future self-improvement *genome* knob).
# --------------------------------------------------------------------------------------------------
# Each of the four factors is first mapped to a [0,1] sub-score (1.0 == maximally confidence-raising),
# then combined as a weighted average: confidence = sum(w_i * f_i) / sum(w_i). Keeping all four weights
# (plus the margin squash constant and the level thresholds) in one dict means a proposed retune of the
# confidence policy is a single, frozen-A/B-gateable diff -- exactly like bestofn.WEIGHTS. The relative
# magnitudes encode the intended emphasis: selftest and refuter-survival (hard correctness evidence)
# weigh most, consensus and margin (agreement signals best-of-N uniquely provides) weigh less because a
# confident consensus can still be a consensus of wrong answers.
CONF_WEIGHTS: dict[str, float] = {
    "selftest": 0.40,        # winner's own self-test: True->1.0, None->0.5 (neutral), False->0.0
    "consensus": 0.20,       # cluster_size / N : how much of the fleet converged on the winner
    "refuter": 0.25,         # winner survival fraction in [0,1]; 0.5 when no refuters ran
    "margin": 0.15,          # winner score minus runner-up score, squashed to [0,1]
    # --- tuning constants (also part of the genome surface) ---
    "margin_k": 40.0,        # bounded squash constant: margin / (margin + k). k ~ one selftest-gap
                             #   scale so a "clear lead" (e.g. a lone passing test) squashes near ~0.7+
                             #   while a near-tie stays near 0.
    "high_thresh": 0.75,     # confidence >= this -> level "high"
    "low_thresh": 0.40,      # confidence <  this -> level "low"
}


def _selftest_subscore(passed) -> float:
    """Map the winner's selftest_passed to [0,1]: True->1.0, None->0.5 (neutral), False->0.0."""
    if passed is True:
        return 1.0
    if passed is False:
        return 0.0
    return 0.5


def _margin_subscore(margin: float, k: float) -> float:
    """Bounded squash of a non-negative score margin into [0,1] via margin / (margin + k).

    Monotonic, 0 at margin==0 (a tie -> no confidence from this factor), asymptotic to 1. A small k
    makes even a modest lead register; k is a CONF_WEIGHTS knob. Negative margins (should not occur,
    since the winner is the top-scored) are floored at 0.
    """
    m = max(0.0, float(margin))
    if k <= 0:
        return 1.0 if m > 0 else 0.0
    return m / (m + k)


def _winner_survival(winner: dict) -> float:
    """Refuter survival fraction of the winner in [0,1]; 0.5 (neutral) when no refuters ran.

    Reuses bestofn's exact definition so the two modules never drift.
    """
    return bestofn._refuter_survival(winner)


def task_confidence(candidates: Iterable[dict], *, weights=None) -> dict:
    """Compute a calibrated confidence + abstention decision for the best-of-N winner.

    Runs relay.bestofn.select_best to pick the winner + ranking + consensus, then blends four factors
    (selftest, consensus fraction, refuter survival, score margin -- see module docstring and
    CONF_WEIGHTS) into a single confidence in [0,1].

    Args:
        candidates: iterable of candidate dicts (relay/bestofn.py shape).
        weights:    optional override for bestofn.WEIGHTS (the *selector* weights), forwarded to
                    select_best. The *confidence* policy weights are CONF_WEIGHTS (module-level).

    Returns:
        {
            "confidence":          float in [0,1],
            "level":               "high" | "medium" | "low",
            "abstain":             bool,   # True => do not silently ship; escalate
            "winner_idx":          int | None,
            "n_candidates":        int,
            "consensus_fraction":  float in [0,1],
            "rationale":           str naming the deciding factors,
        }
    """
    cands = list(candidates)
    cw = CONF_WEIGHTS

    # Defensive: no candidates -> maximally humble, abstain.
    if not cands:
        return {
            "confidence": 0.0,
            "level": "low",
            "abstain": True,
            "winner_idx": None,
            "n_candidates": 0,
            "consensus_fraction": 0.0,
            "rationale": "no candidates",
        }

    n = len(cands)
    sel = bestofn.select_best(cands, weights=weights)
    winner = sel["winner"]
    ranking = sel["ranking"]

    # select_best returns a winner whenever cands is non-empty; guard anyway.
    if winner is None:
        return {
            "confidence": 0.0,
            "level": "low",
            "abstain": True,
            "winner_idx": None,
            "n_candidates": n,
            "consensus_fraction": 0.0,
            "rationale": "no winner",
        }

    winner_idx = winner.get("idx")

    # consensus fraction: winner's cluster size / N. ranking[0] is the winner (best -> worst).
    winner_entry = ranking[0]
    winner_cluster = int(winner_entry.get("consensus_size", 1) or 1)
    consensus_fraction = winner_cluster / n if n > 0 else 0.0

    # score margin: winner score minus runner-up score (0 if the winner is the only candidate).
    winner_score = float(winner_entry.get("score", 0.0))
    if len(ranking) >= 2:
        runner_score = float(ranking[1].get("score", 0.0))
        margin = winner_score - runner_score
    else:
        margin = 0.0

    # --- four factor sub-scores in [0,1] ---
    st_passed = winner.get("selftest_passed", None)
    f_selftest = _selftest_subscore(st_passed)
    f_consensus = max(0.0, min(1.0, consensus_fraction))
    f_refuter = max(0.0, min(1.0, _winner_survival(winner)))
    f_margin = _margin_subscore(margin, float(cw["margin_k"]))

    # Empty-diff / no-real-candidate winner: bestofn floors its score; confidence must be very low.
    if bestofn._is_empty(winner.get("diff", "")):
        confidence = 0.0
        level = "low"
        abstain = True
        rationale = "winner has empty diff (no real change) -> minimal confidence"
        return {
            "confidence": confidence,
            "level": level,
            "abstain": abstain,
            "winner_idx": winner_idx,
            "n_candidates": n,
            "consensus_fraction": consensus_fraction,
            "rationale": rationale,
        }

    # --- weighted-average blend, then clamp to [0,1] ---
    w_sel = float(cw["selftest"])
    w_con = float(cw["consensus"])
    w_ref = float(cw["refuter"])
    w_mar = float(cw["margin"])
    wsum = w_sel + w_con + w_ref + w_mar
    if wsum <= 0:
        wsum = 1.0
    raw = (
        w_sel * f_selftest
        + w_con * f_consensus
        + w_ref * f_refuter
        + w_mar * f_margin
    ) / wsum
    confidence = max(0.0, min(1.0, raw))

    # --- level ---
    high_thresh = float(cw["high_thresh"])
    low_thresh = float(cw["low_thresh"])
    if confidence >= high_thresh:
        level = "high"
    elif confidence < low_thresh:
        level = "low"
    else:
        level = "medium"

    # --- abstain ---
    # Abstain when we are NOT confident (level low) AND have no hard self-test pass to lean on. A
    # passing self-test is hard evidence the patch works, so even a low blended score should not force
    # abstention if that test passed; conversely a low score with no pass means "escalate".
    abstain = (level == "low") and (st_passed is not True)

    rationale = _confidence_rationale(
        st_passed, winner_cluster, n, winner, margin, f_margin, confidence, level
    )

    return {
        "confidence": confidence,
        "level": level,
        "abstain": abstain,
        "winner_idx": winner_idx,
        "n_candidates": n,
        "consensus_fraction": consensus_fraction,
        "rationale": rationale,
    }


def _confidence_rationale(
    st_passed, cluster: int, n: int, winner: dict, margin: float, f_margin: float,
    confidence: float, level: str,
) -> str:
    """Short string naming the factors that drove the confidence + level."""
    parts: list[str] = []
    if st_passed is True:
        parts.append("self-test passed")
    elif st_passed is False:
        parts.append("self-test FAILED")
    else:
        parts.append("self-test unknown")

    parts.append("%d/%d converged" % (cluster, n))

    total = int(winner.get("refuter_total", 0) or 0)
    if total > 0:
        survived = total - max(0, min(int(winner.get("refuter_refuted", 0) or 0), total))
        parts.append("refuters %d/%d survived" % (survived, total))
    else:
        parts.append("no refuters")

    if n >= 2:
        if f_margin >= 0.5:
            parts.append("clear lead (margin %.1f)" % margin)
        elif margin <= 0.0:
            parts.append("near-tie with runner-up")
        else:
            parts.append("modest lead (margin %.1f)" % margin)
    else:
        parts.append("single candidate (no runner-up)")

    return "%s -> confidence %.2f (%s)" % ("; ".join(parts), confidence, level)


def should_escalate(conf: dict) -> bool:
    """True iff the confidence result says the patch must not be silently shipped.

    A router consumes this to decide what to do instead of shipping a low-confidence patch:
      (a) WIDEN N / verify harder -- fan out more attempts or spawn more adversarial refuters to gather
          additional signal before committing; or
      (b) SURFACE DIVERGENT OPTIONS to a human -- when the N diverged into ties / no consensus, present
          the competing candidates rather than silently picking one.

    Escalate when the result abstains, or when its level is "low" (the two overlap by design, but we
    treat either as a trigger so a low-but-non-abstaining result -- e.g. low score yet a passing
    self-test -- still routes to extra verification rather than silent shipping).
    """
    return bool(conf.get("abstain")) or conf.get("level") == "low"


def explain(conf: dict, select_result: dict) -> str:
    """Short human-facing transparency string. Never raises.

    Example: "picked candidate #2 of 5 (confidence 0.82 high): self-test passed, 4/5 converged,
    refuters 2/2 survived". `select_result` is a relay.bestofn.select_best output -- accept it as a
    param so the caller reuses the single selection rather than recomputing it.
    """
    try:
        winner_idx = conf.get("winner_idx")
        n = conf.get("n_candidates", 0)
        confidence = conf.get("confidence", 0.0)
        level = conf.get("level", "low")

        if winner_idx is None:
            return "abstained: no candidate to ship (confidence %.2f %s)" % (
                float(confidence), level
            )

        sel = select_result if isinstance(select_result, dict) else {}
        winner = sel.get("winner") or {}
        ranking = sel.get("ranking") or []

        # consensus cluster of the winner from the selection (fall back to conf fraction).
        cluster = None
        if ranking:
            cluster = int(ranking[0].get("consensus_size", 1) or 1)
        if cluster is None:
            frac = float(conf.get("consensus_fraction", 0.0))
            cluster = int(round(frac * n)) if n else 0

        bits: list[str] = []
        st = winner.get("selftest_passed", None)
        if st is True:
            bits.append("self-test passed")
        elif st is False:
            bits.append("self-test failed")
        else:
            bits.append("self-test not run")

        bits.append("%d/%d converged" % (cluster, n))

        total = int(winner.get("refuter_total", 0) or 0)
        if total > 0:
            survived = total - max(0, min(int(winner.get("refuter_refuted", 0) or 0), total))
            bits.append("refuters %d/%d survived" % (survived, total))

        head = "picked candidate #%s of %s (confidence %.2f %s)" % (
            winner_idx, n, float(confidence), level
        )
        line = "%s: %s" % (head, ", ".join(bits))
        if conf.get("abstain"):
            line += " -- ABSTAIN: escalate, do not silently ship"
        return line
    except Exception:
        # Transparency string must never crash a caller.
        return "explain unavailable"
