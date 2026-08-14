"""One turn of the closed loop, with every gate wired in the order that makes it safe.

The pieces existed separately and could each be used correctly or not. This is the single
entry point that uses them correctly, so a caller cannot accidentally build a version that
skips the ledger, activates on INCONCLUSIVE, or grades a run whose judge had changed.

    propose  ->  evaluate  ->  decide  ->  record  ->  maybe activate

The ordering constraints are load-bearing and each one is a defect somebody would otherwise
reintroduce:

  * the hypothesis is written BEFORE evaluation, or there is no hypothesis;
  * frozen integrity is checked before AND after, because a long run can be tampered with
    mid-flight and a judge that changed invalidates the result retroactively;
  * only KEEP may activate -- and activation writes the manifest, not "the intent to";
  * the conclusion is appended whatever the outcome, including the ones nobody wants to
    look at. An experiment that failed and was never concluded is indistinguishable from
    one that is still running, and both of those look like progress.

This module deliberately does NOT know how to evaluate. It takes an evaluate callable and
calls it once. Anything that both proposes a change and decides whether it worked has no
judge, and the whole structure exists to keep those apart.
"""
from __future__ import annotations

from relay.selfimprove import decision as D
from relay.selfimprove import experiment as EX
from relay.selfimprove import frozen as F
from relay.selfimprove import manifest as M
from relay.selfimprove import runtime_config as RC
from relay.selfimprove.ledger import HypothesisLedger


class EvolutionController:
    """Runs one candidate end to end. Stateless between calls except for its stores."""

    def __init__(self, *, ledger=None, archive=None, baseline_path=None,
                 activate=False, auto_apply=False):
        self.ledger = ledger or HypothesisLedger()
        self.archive = archive
        self.baseline_path = baseline_path
        # `activate` is the operator's switch: even a KEEP does not change the running
        # harness unless this is on. Defaulting it to False means the safe path is the one
        # you get by not thinking about it.
        self.activate = bool(activate)
        self.auto_apply = bool(auto_apply)

    def run_candidate(self, *, genome, hypothesis, target_failure_class, evaluate,
                      evidence=None, predicted_effect=None, possible_regressions=None,
                      evaluation_plan=None, base=None) -> dict:
        """Propose, evaluate, decide, record. Returns the full outcome.

        `evaluate(manifest, experiment_id)` must return a dict with any of: gate, sentinel,
        security, regression, infra, plus whatever measurements it wants carried into the
        conclusion. It is called EXACTLY once -- an evaluator invoked twice invites
        picking the better run, which is the same defect as an editable hypothesis.
        """
        base = base or M.base_manifest()
        candidate = M.apply_genome(base, genome or {})
        parent_id = M.harness_id(base)
        cand_id = M.harness_id(candidate)
        changed = M.diff(base, candidate)

        experiment_id = EX.new_experiment_id()
        self.ledger.propose(
            experiment_id=experiment_id,
            candidate_id=EX.candidate_id(genome or {}, parent_harness_id=parent_id),
            parent_harness_id=parent_id,
            target_failure_class=target_failure_class,
            evidence=evidence,
            hypothesis=hypothesis,
            changed_components=sorted(changed),
            predicted_effect=predicted_effect,
            possible_regressions=possible_regressions,
            evaluation_plan=evaluation_plan,
        )

        ok_pre, changed_pre = self._frozen()
        if not ok_pre:
            return self._conclude(experiment_id, D.decide(frozen_ok=False),
                                  {"frozen_changed": changed_pre}, candidate, cand_id,
                                  changed)

        try:
            result = evaluate(candidate, experiment_id) or {}
        except Exception as exc:
            # An evaluator that raised told us nothing about the candidate. Recording that
            # as a rejection would blame the change for the harness's own failure.
            return self._conclude(
                experiment_id,
                D.decide(infra={"aborted": True,
                                "reason": "evaluator raised %s: %s" % (type(exc).__name__, exc)}),
                {"exception": type(exc).__name__}, candidate, cand_id, changed)

        ok_post, changed_post = self._frozen()
        if not ok_post:
            return self._conclude(experiment_id, D.decide(frozen_ok=False),
                                  {"frozen_changed_during_run": changed_post},
                                  candidate, cand_id, changed)

        verdict = D.decide(
            gate=result.get("gate"),
            sentinel=result.get("sentinel"),
            security=result.get("security"),
            regression=result.get("regression"),
            infra=result.get("infra"),
            frozen_ok=True,
            auto_apply=self.auto_apply,
            # The requirements follow the ACT, not the flag that automated it. Passing
            # activate=True with auto_apply=False used to skip the security requirement and
            # then write the manifest anyway.
            will_activate=self.activate,
        )
        return self._conclude(experiment_id, verdict, result, candidate, cand_id, changed)

    # -- internals ---------------------------------------------------------------------

    def _frozen(self):
        try:
            if self.baseline_path:
                return F.frozen_intact(baseline_path=self.baseline_path)
            return F.frozen_intact()
        except Exception as exc:
            # Unable to check is not the same as intact, and must not be treated as it.
            return False, ["frozen check failed: %s" % exc]

    def _conclude(self, experiment_id, verdict, result, candidate, cand_id, changed):
        self.ledger.conclude(
            experiment_id=experiment_id,
            verdict=_ledger_verdict(verdict["state"]),
            actual_effect=result.get("actual_effect") or {},
            latency_delta=result.get("latency_delta"),
            turn_delta=result.get("turn_delta"),
            tool_call_delta=result.get("tool_call_delta"),
            security_delta=result.get("security_delta"),
            infra_delta=result.get("infra_delta"),
            note=verdict["reason"],
        )
        activated = False
        if verdict["may_activate"] and self.activate:
            RC.write_active(candidate)
            activated = True
        if self.archive is not None:
            try:
                self.archive.add(
                    {"components": candidate["components"],
                     "parameters": candidate["parameters"],
                     "parent_id": None},
                    slice_ids=result.get("slice_ids") or [],
                    pass_at_1=result.get("pass_at_1"),
                    gate_verdict=verdict["state"],
                    descriptors={"experiment_id": experiment_id,
                                 "harness_id": cand_id,
                                 "changed": changed},
                )
            except Exception:
                # A full archive write failing must not lose the decision itself, which is
                # already durable in the ledger.
                pass
        return {
            "experiment_id": experiment_id,
            "harness_id": cand_id,
            "changed": changed,
            "decision": verdict,
            "activated": activated,
            "result": result,
        }


#: The decision states map onto the ledger's smaller vocabulary. Every rejection reason is
#: preserved in the conclusion's `note`, so collapsing here loses the label but not the
#: reason -- and the decision state is kept verbatim in the archive row.
_LEDGER_MAP = {
    D.KEEP: "keep",
    D.REJECT: "reject",
    D.INCONCLUSIVE: "inconclusive",
    D.INFRA_ABORT: "infra_abort",
    D.SECURITY_REJECT: "reject",
    D.SENTINEL_REJECT: "reject",
    D.REGRESSION_REJECT: "reject",
    D.NEEDS_HUMAN_REVIEW: "needs_human_review",
}


def _ledger_verdict(state):
    return _LEDGER_MAP.get(state, "inconclusive")
