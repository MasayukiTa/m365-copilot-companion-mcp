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

from relay import provenance as PROV
from relay.selfimprove import decision as D
from relay.selfimprove import experiment as EX
from relay.selfimprove import frozen as F
from relay.selfimprove import manifest as M
from relay.selfimprove import qd as QD
from relay.selfimprove import runtime_config as RC
from relay.selfimprove.ledger import HypothesisLedger


def _target_of(result) -> str:
    """The execution target an evaluator reported, tolerating the older string shape.

    `agent` used to be a bare class name and is now a description dict. An archive write
    must not raise on a result produced by either -- the row is the durable record, and a
    format quibble is not a reason to lose it.
    """
    agent = (result or {}).get("agent")
    if isinstance(agent, dict):
        return agent.get("execution_target", "")
    return ""


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
        self._parent_id = ""

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
        self._parent_id = parent_id          # the archive needs it; it was writing None
        cand_id = M.harness_id(candidate)
        changed = M.diff(base, candidate)

        # WHAT THIS PROPOSAL IS BUILT ON, declared rather than assumed -- and NOT supplied on
        # the caller's behalf. An earlier version defaulted an absent `evidence` to a record
        # asserting AGENT_INFERENCE, "no external evidence cited". That is an assertion about
        # the caller's reasoning made by the callee, which cannot see it, and it converted the
        # provenance check into a formality: the one route that mattered -- external text
        # reaching a harness mutation -- was open to anyone who simply passed nothing, and the
        # refusal for unevidenced proposals downstream became unreachable code.
        #
        # So an absent list stays absent, and the check refuses it. A caller whose proposal
        # really does rest on its own measurements says so; that is one line, and it is the
        # line that makes the answer mean anything.
        evidence = list(evidence or [])

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
            # THE BASE THE CONTROLLER RECORDED AS THE PARENT, handed to the evaluator so the
            # comparison is against the harness the record names. make_evaluator grew an
            # optional `base` argument and nothing passed it, so a run could record parent A
            # while comparing against the evaluator's own default B -- a wrong record, which
            # is worse than a missing one. Evaluators that do not accept it are still called
            # the old way rather than being broken.
            try:
                result = evaluate(candidate, experiment_id, base=base) or {}
            except TypeError:
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

    def _archive(self, experiment_id, verdict, result, candidate, cand_id, changed):
        """Write the durable experiment record. Returns "" on success, the error otherwise.

        WHAT GOES IN IS THE EVIDENCE, not a summary of it. The row carried a pass count, the
        slice ids and three descriptors -- so an archived experiment could not answer which
        episodes moved, whether security held, or which harness produced it, and the brief's
        first principle asks exactly those. parent_id was written as None while the parent
        was sitting in a local variable.
        """
        if self.archive is None:
            return ""
        try:
            self.archive.add(
                {"components": candidate["components"],
                 "parameters": candidate["parameters"],
                 "parent_id": result.get("parent_harness_id") or self._parent_id},
                slice_ids=result.get("slice_ids") or [],
                pass_at_1=result.get("pass_at_1"),
                gate_verdict=verdict["state"],
                descriptors={
                    # -- identity: which experiment, which harnesses, which candidate ------
                    "experiment_id": experiment_id,
                    "candidate_id": EX.candidate_id(
                        {"components": candidate["components"],
                         "parameters": candidate["parameters"]},
                        parent_harness_id=self._parent_id),
                    "harness_id": cand_id,
                    "candidate_harness_id": cand_id,
                    "parent_harness_id": self._parent_id,
                    "baseline_harness_id": (result.get("baseline_harness_id")
                                            or self._parent_id),
                    "components": dict(candidate["components"]),
                    "parameters": dict(candidate["parameters"]),
                    "changed": changed,
                    # -- verdict -----------------------------------------------------------
                    "decision_state": verdict["state"],
                    "decision_reason": verdict.get("reason", ""),
                    # -- the evidence, not a summary of it ---------------------------------
                    # A review asked what an archived row can actually answer, and the
                    # answer was "which ids passed" -- not which episodes moved, what they
                    # scored, what the graders saw, or what the suite even was. Every field
                    # below exists because reconstructing a past comparison needed it and
                    # it was not there.
                    "paired_ids": result.get("paired_ids") or [],
                    "on": result.get("on") or {},
                    "off": result.get("off") or {},
                    "episode_results": {
                        "candidate": result.get("candidate_results") or [],
                        "baseline": result.get("baseline_results") or [],
                    },
                    "security": result.get("security") or {},
                    "sentinel": result.get("sentinel") or {},
                    "regression": result.get("regression") or {},
                    "infra": result.get("infra") or {},
                    # -- what produced it --------------------------------------------------
                    "pools": result.get("pools") or {},
                    "grader_version": result.get("grader_version", ""),
                    "seed": result.get("seed"),
                    "agent": result.get("agent", ""),
                    "latency_s": result.get("latency_s"),
                    # BOTH SIDES, IN FULL. Only the candidate had a detailed fingerprint;
                    # the baseline had an id, so a reader could see that two harnesses
                    # differed and not why -- which is the question a fingerprint exists to
                    # answer. The execution target comes from the adapter rather than being
                    # left empty, since "which target ran this" decides what the numbers
                    # cover.
                    # BEHAVIOUR, not size. archive.descriptors bins by diff size and turn
                    # count, which are properties of the EPISODE here rather than of the
                    # harness -- so two genomes that behave completely differently land in
                    # one cell and the QD map keeps one elite at some expense. See
                    # relay/selfimprove/qd.py.
                    "semantic": QD.descriptors(
                        (result.get("candidate_results") or [])),
                    "harness_fingerprint": EX.harness_fingerprint(
                        genome={"components": candidate["components"],
                                "parameters": candidate["parameters"]},
                        execution_profile=_target_of(result)),
                    "baseline_fingerprint": EX.harness_fingerprint(
                        genome=(result.get("baseline_genome")
                                or {"components": {}, "parameters": {}}),
                        execution_profile=_target_of(result)),
                    "dataset_fingerprint": result.get("dataset_fingerprint", ""),
                },
            )
        except Exception as exc:
            return "%s: %s" % (type(exc).__name__, exc)
        return ""

    def _conclude(self, experiment_id, verdict, result, candidate, cand_id, changed):
        # ARCHIVE FIRST, THEN CONCLUDE, THEN ACTIVATE. The ledger conclusion was written
        # before the archive attempt, and a failed archive then downgraded the RETURNED
        # verdict from KEEP to NEEDS_HUMAN_REVIEW -- leaving the durable record saying
        # "keep" and the caller saying "review". The durable record is the one that survives
        # the session, so it is the one that must not be wrong.
        archive_error = self._archive(experiment_id, verdict, result, candidate, cand_id,
                                      changed)
        if archive_error and verdict["may_activate"]:
            verdict = dict(verdict, state=D.NEEDS_HUMAN_REVIEW, may_activate=False,
                           reason="the experiment record could not be written (%s); "
                                  "activating without it would leave a live harness change "
                                  "nobody can attribute" % archive_error)
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
        return {
            "experiment_id": experiment_id,
            "harness_id": cand_id,
            "changed": changed,
            "decision": verdict,
            "activated": activated,
            "archive_error": archive_error,
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
