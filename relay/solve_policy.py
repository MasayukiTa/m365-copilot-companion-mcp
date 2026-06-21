"""Solve-policy ROUTER -- the capstone that ties the strengths into one "how to solve, then decide".

This is where Bet #1 (best-of-N) and Bet #2 (calibration) meet as a single policy a caller can drive:

    plan = plan_solve(instance_id, calibration_report)     # HOW: best-of-N (with N diverse genomes) or
                                                           #      single-shot, from MEASURED competence
    # ... the FLEET runs plan["genomes"] (1 or N attempts) and captures one prediction per attempt ...
    decision = finalize(pred_records)                      # WHAT TO SHIP: winner + confidence + abstain

The router is PURE policy: it decides how many attempts and with which (diverse) genomes, and how to
pick + how-confident the result is. The actual N-parallel solve is fleet-heavy and lives elsewhere
(post-measurement); this module never solves anything. Composes, READ-ONLY:
  - relay.selfimprove.calibration.recommend_effort  -- spend best-of-N where MEASURED-weak.
  - relay.selfimprove.diversify.diversify           -- the N diverse genomes for a best-of-N run.
  - relay.selfimprove.apply.active_genome           -- the incumbent base scaffold to diversify from.
  - relay.bestofn_run.decide                         -- N captures -> winner + confidence + abstain.
"""
from __future__ import annotations

from relay.selfimprove import calibration as _cal
from relay.selfimprove import diversify as _div
from relay.selfimprove import apply as _apply
from relay import bestofn_run as _bn


def plan_solve(instance_id, report=None, *, n=4, base_genome=None,
               grade_results_path=None) -> dict:
    """Decide HOW to solve this task, from the agent's MEASURED competence on its class.

    - class = calibration.classify_instance(instance_id) (repo owner).
    - mode  = calibration.recommend_effort(report, class): "best-of-N" for measured-weak / data-poor
              classes, "single-shot" for measured-strong ones (Bet #2's payoff -- spend cheap
              parallelism where the agent is provably weak).
    - if best-of-N: genomes = diversify(base, n) (attempt 0 = the incumbent base; 1..N-1 distinct
                    domain-general variants). n shrinks if the generator runs dry.
    - if single-shot: genomes = [base], one attempt.

    `report` defaults to calibration_report(grade_results_path); `base_genome` defaults to the currently
    applied scaffold (apply.active_genome()). Returns a plan dict ready to hand to the fleet:
      {"instance_id","task_class","mode","n","genomes","reason"}.
    Defensive: never raises; on any trouble it falls back to a safe single-shot plan.
    """
    try:
        if report is None:
            report = _cal.calibration_report(grade_results_path)
        base = base_genome if base_genome is not None else _apply.active_genome()
        task_class = _cal.classify_instance(instance_id)
        rec = _cal.recommend_effort(report, task_class)
        mode = rec.get("mode", "best-of-N")
        reason = rec.get("reason", "")

        if mode == "best-of-N":
            genomes = _div.diversify(base, n)
            return {
                "instance_id": instance_id,
                "task_class": task_class,
                "mode": "best-of-N",
                "n": len(genomes),
                "genomes": genomes,
                "reason": reason,
            }
        return {
            "instance_id": instance_id,
            "task_class": task_class,
            "mode": "single-shot",
            "n": 1,
            "genomes": [base],
            "reason": reason,
        }
    except Exception:
        # Safe fallback: a single-shot plan on the (possibly empty) base.
        base = base_genome if base_genome is not None else {"knobs": {}, "cards": {},
                                                            "parent_id": None, "note": "base"}
        return {
            "instance_id": instance_id,
            "task_class": _safe_class(instance_id),
            "mode": "single-shot",
            "n": 1,
            "genomes": [base],
            "reason": "fallback: policy unavailable, defaulting to single-shot",
        }


def _safe_class(instance_id) -> str:
    try:
        return _cal.classify_instance(instance_id)
    except Exception:
        return "unknown"


def finalize(pred_records, *, weights=None) -> dict:
    """Given the captured attempts (1 for single-shot, N for best-of-N), decide what to ship.

    Delegates to bestofn_run.decide, which works for any N >= 1: a single capture is simply selected
    and scored (its confidence comes from its own self-test / refuter signals, with no consensus or
    margin to draw on -- exactly right for single-shot). Returns the decide() result: winner
    {instance_id,diff} | None, confidence/level/abstain/escalate, ranking, explain.
    """
    return _bn.decide(pred_records, weights=weights)


def plan_and_explain(instance_id, report=None, *, n=4, grade_results_path=None) -> str:
    """One-line human summary of the plan (for a cockpit / log). Never raises."""
    try:
        p = plan_solve(instance_id, report, n=n, grade_results_path=grade_results_path)
        if p["mode"] == "best-of-N":
            return "%s [%s]: best-of-%d (%s)" % (instance_id, p["task_class"], p["n"], p["reason"])
        return "%s [%s]: single-shot (%s)" % (instance_id, p["task_class"], p["reason"])
    except Exception:
        return "%s: single-shot (fallback)" % instance_id
