"""The hypothesis ledger: what we expected, written down BEFORE we looked.

A record written after the fact is not evidence, it is a story. If the prediction can be
edited once the numbers are in, then every experiment succeeds -- the hypothesis simply
becomes whatever happened. This is the oldest failure mode in empirical work and an
automated proposer is far more prone to it than a person, because it will happily
rationalise any outcome on request.

So the ledger has exactly two operations:

    propose(...)   write the hypothesis. Fails if this candidate already has one.
    conclude(...)  append the observed effect and a verdict. Never touches the hypothesis.

Both append a line to a JSONL file. Nothing rewrites a line, nothing deletes one, and a
conclusion that arrives twice is recorded twice rather than replacing its predecessor --
the second one is itself a fact worth seeing.

WHY THE VERDICT HAS FIVE VALUES

"kept" and "rejected" cannot express the outcome that actually dominates: the experiment
ran, the numbers moved a little, and the difference is inside the noise. Recording that as
a rejection quietly teaches the optimiser that the change was harmful, and recording it as
a keep is worse. INCONCLUSIVE exists so the common case can be told the truth. INFRA_ABORT
separates "we learned nothing because the harness broke" from "we learned nothing because
the change did nothing", and those must never be pooled.
"""
from __future__ import annotations

import json
import os
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_PATH = os.path.join(REPO, ".fleet", "selfimprove", "hypotheses.jsonl")

KEEP = "keep"
REJECT = "reject"
INCONCLUSIVE = "inconclusive"
INFRA_ABORT = "infra_abort"
NEEDS_HUMAN_REVIEW = "needs_human_review"
VERDICTS = (KEEP, REJECT, INCONCLUSIVE, INFRA_ABORT, NEEDS_HUMAN_REVIEW)

PROPOSAL = "proposal"
CONCLUSION = "conclusion"


class LedgerError(RuntimeError):
    """Raised for the two things that must never happen: a rewritten hypothesis, or a
    conclusion about an experiment that was never proposed."""


class HypothesisLedger:
    """Append-only record of predictions and what actually happened.

    Reads the whole file on construction. That is fine at this scale and it buys the
    duplicate-proposal check, which is the property worth having: an optimiser that can
    re-propose the same candidate with a different hypothesis after seeing results has
    defeated the point of writing anything down.
    """

    def __init__(self, path: str = DEFAULT_PATH):
        self.path = path
        self._rows: list[dict] = []
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._rows.append(json.loads(line))
                    except Exception:
                        # A corrupt line is skipped rather than fatal: losing the ability to
                        # record NEW experiments because an old line is malformed would be a
                        # worse outcome than one unreadable row.
                        continue

    # -- writing ---------------------------------------------------------------------

    def propose(self, *, experiment_id, candidate_id, parent_harness_id="",
                target_failure_class="", evidence=None, hypothesis="",
                changed_components=None, predicted_effect=None,
                possible_regressions=None, evaluation_plan=None, ts=None) -> dict:
        """Record what we expect, before running anything.

        `hypothesis` and `target_failure_class` are required in substance, not just in
        form: a proposal that cannot say what it expects to fix is not a hypothesis, it is
        a change looking for a justification.
        """
        if not experiment_id or not candidate_id:
            raise LedgerError("a proposal needs both experiment_id and candidate_id")
        if not str(hypothesis).strip():
            raise LedgerError("a proposal without a hypothesis is a change looking for a "
                              "justification; say what you expect and why")
        if not str(target_failure_class).strip():
            raise LedgerError("a proposal must name the failure class it targets")
        if self.proposal_for(experiment_id) is not None:
            raise LedgerError("experiment %s already has a proposal; the ledger is "
                              "append-only and a hypothesis is never rewritten"
                              % experiment_id)
        row = {
            "kind": PROPOSAL,
            "experiment_id": experiment_id,
            "candidate_id": candidate_id,
            "parent_harness_id": parent_harness_id,
            "target_failure_class": target_failure_class,
            "evidence": list(evidence or []),
            "hypothesis": hypothesis,
            "changed_components": list(changed_components or []),
            "predicted_effect": dict(predicted_effect or {}),
            "possible_regressions": list(possible_regressions or []),
            "evaluation_plan": dict(evaluation_plan or {}),
            "ts": int(time.time() if ts is None else ts),
        }
        self._append(row)
        return row

    def conclude(self, *, experiment_id, verdict, actual_effect=None, latency_delta=None,
                 turn_delta=None, tool_call_delta=None, security_delta=None,
                 infra_delta=None, note="", ts=None) -> dict:
        """Append what was observed. The proposal is left exactly as written."""
        if verdict not in VERDICTS:
            raise LedgerError("unknown verdict %r; expected one of %s"
                              % (verdict, ", ".join(VERDICTS)))
        if self.proposal_for(experiment_id) is None:
            raise LedgerError("no proposal for %s: a conclusion without a prior hypothesis "
                              "is a result in search of a prediction" % experiment_id)
        row = {
            "kind": CONCLUSION,
            "experiment_id": experiment_id,
            "verdict": verdict,
            "actual_effect": dict(actual_effect or {}),
            "latency_delta": latency_delta,
            "turn_delta": turn_delta,
            "tool_call_delta": tool_call_delta,
            "security_delta": security_delta,
            "infra_delta": infra_delta,
            "note": note,
            "ts": int(time.time() if ts is None else ts),
        }
        self._append(row)
        return row

    def _append(self, row: dict) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._rows.append(row)

    # -- reading ---------------------------------------------------------------------

    def proposal_for(self, experiment_id) -> dict | None:
        for row in self._rows:
            if row.get("kind") == PROPOSAL and row.get("experiment_id") == experiment_id:
                return row
        return None

    def conclusions_for(self, experiment_id) -> list:
        """All of them, in order. A second conclusion does not replace the first -- that it
        arrived at all is a fact about the experiment."""
        return [r for r in self._rows
                if r.get("kind") == CONCLUSION and r.get("experiment_id") == experiment_id]

    def open_experiments(self) -> list:
        """Proposed but never concluded. These are the ones quietly rotting."""
        concluded = {r["experiment_id"] for r in self._rows if r.get("kind") == CONCLUSION}
        return [r["experiment_id"] for r in self._rows
                if r.get("kind") == PROPOSAL and r["experiment_id"] not in concluded]

    def all(self) -> list:
        return list(self._rows)

    def prediction_accuracy(self) -> dict:
        """How often a prediction survived contact with the measurement.

        Not a score to optimise -- it is a check on the PROPOSER. A proposer whose
        hypotheses are almost never borne out is generating plausible text rather than
        reasoning about the harness, and that is invisible if only outcomes are tracked.
        """
        counts = {v: 0 for v in VERDICTS}
        for row in self._rows:
            if row.get("kind") == CONCLUSION:
                counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
        decided = counts[KEEP] + counts[REJECT]
        return {
            "counts": counts,
            "decided": decided,
            "keep_rate": (counts[KEEP] / decided) if decided else None,
            "open": len(self.open_experiments()),
        }
