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

import contextlib
import json
import os
import time

from relay import provenance as PROV

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_PATH = os.path.join(REPO, ".fleet", "selfimprove", "hypotheses.jsonl")

#: Redirects the ledger, and is READ AT CONSTRUCTION rather than bound as a default argument.
#: `nightly()` builds its controller internally, so a test calling it had no way to point the
#: ledger anywhere -- and measured, one such test run added 120 rows to the production file.
#: conftest points this at a temp path for the whole suite; an operator can point it at a
#: scratch ledger for a rehearsal.
ENV_PATH = "MCP_SELFIMPROVE_HYPOTHESES"


def default_path() -> str:
    return os.environ.get(ENV_PATH) or DEFAULT_PATH

KEEP = "keep"
REJECT = "reject"
INCONCLUSIVE = "inconclusive"
INFRA_ABORT = "infra_abort"
NEEDS_HUMAN_REVIEW = "needs_human_review"
VERDICTS = (KEEP, REJECT, INCONCLUSIVE, INFRA_ABORT, NEEDS_HUMAN_REVIEW)

PROPOSAL = "proposal"
CONCLUSION = "conclusion"

#: What a line that will not parse becomes when the file is re-read. It is kept rather than
#: dropped: a corrupt line is evidence about the ledger's history, and silently skipping it
#: makes an audit read clean when it is not.
CORRUPT = "corrupt"


@contextlib.contextmanager
def _exclusive(lock_path):
    """A cross-process exclusive lock, held for the duration of an append.

    O_CREAT|O_EXCL rather than a library, because this has to work identically on Windows
    (where fcntl does not exist) and the critical section is two file operations long. A
    stale lock from a killed process is broken after a bounded wait -- refusing to ever
    write again would be a worse failure than a rare double-append, and the re-read inside
    the section catches that case anyway.
    """
    deadline = time.time() + 10.0
    fd = None
    while fd is None:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.time() > deadline:
                try:
                    os.unlink(lock_path)          # presumed stale; the re-read still guards
                except OSError:
                    pass
                deadline = time.time() + 10.0
            time.sleep(0.02)
    try:
        yield
    finally:
        os.close(fd)
        try:
            os.unlink(lock_path)
        except OSError:
            pass


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

    def __init__(self, path: str | None = None):
        # RESOLVED HERE, NOT IN THE SIGNATURE. A default of `path=DEFAULT_PATH` binds the
        # module attribute at import, which is how the redirect above would have been
        # silently ineffective -- the same trap frozen.py records being caught by, and the
        # same one the authority ledger hit last night.
        self.path = path or default_path()
        self._rows: list[dict] = []
        if os.path.isfile(self.path):
            with open(self.path, encoding="utf-8") as fh:
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
        # PROVENANCE, AT THE POINT WHERE EVIDENCE BECOMES AUTHORITY. This is the step the
        # brief's lineage-poisoning chain ends at: external content reaches a solver, gets
        # summarised into memory or a failure analysis, and is then cited as the reason to
        # change the harness -- after which a single poisoned document is shaping tasks it
        # never touched, permanently, and every later run looks normal.
        #
        # Untrusted evidence stays usable for the task it came from. It cannot be the
        # justification for a mutation. An empty evidence list is refused too, since
        # "cite nothing" would otherwise be the cheapest way around the check.
        try:
            effective_authority = PROV.require_authority_for_evolution(
                evidence, what="experiment %s" % experiment_id)
        except PROV.ProvenanceError as exc:
            raise LedgerError(str(exc)) from exc
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
            "evidence_authority": effective_authority,
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
        """Append under an exclusive lock, re-checking uniqueness against the FILE.

        The duplicate-proposal check ran against this instance's private snapshot, taken
        when it was constructed. Two ledgers built before either wrote could therefore both
        accept the same experiment_id and both append, producing two immutable proposals for
        one experiment -- the exact thing the check exists to prevent, and invisible
        afterwards because both rows look legitimate. The lock plus the re-read make the
        check mean what it says: last writer to arrive loses, rather than both winning.
        """
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with _exclusive(self.path + ".lock"):
            if row.get("kind") == PROPOSAL:
                for existing in self._read_rows_from_disk():
                    if (existing.get("kind") == PROPOSAL
                            and existing.get("experiment_id") == row.get("experiment_id")):
                        raise LedgerError(
                            "experiment %s was proposed by another writer while this one "
                            "was deciding; a hypothesis is never rewritten"
                            % row.get("experiment_id"))
            with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        self._rows.append(row)

    def _read_rows_from_disk(self) -> list:
        """What the file actually contains right now, not what this instance remembers."""
        rows = []
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        rows.append({"kind": CORRUPT, "raw": line[:200]})
        except OSError:
            pass
        return rows

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
