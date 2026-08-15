"""L2 iteration driver: wrap ONE self-improvement iteration with the safety discipline.

This is the L2 rung of the autonomy ladder (bench/SELF_GROWTH_L4_DESIGN.md sec 1, build order #1).
L1 (loop.py) already runs one rigorous A/B VALIDATE iteration and emits a gate verdict; a human then
reviews keep/revert. L2 wraps that single iteration with the *constitutional* discipline that lets it
run unattended without quietly cheating or drifting:

  - frozen-set integrity BEFORE and AFTER the run (sec 0): the judge must be intact; if any frozen
    file changed, the agent may be reward-hacking and the run ABORTS. The post-run re-check catches a
    long run tampered mid-flight.
  - gate + cross-dataset sentinel combination (sec 5): a gain that wins on the slice but regresses on
    a fixed sentinel from a different distribution is flagged as likely grader/dataset-specific and
    NOT kept (the reward-hacking tripwire).
  - archive recording (sec 2): every validated genome is appended to the quality-diversity archive.
  - per-iteration spend ceiling (sec 8): the budget tripwire -- hard stop at the ceiling.
  - default-safe commit policy (sec 1): a passing change QUEUES for human review unless explicitly
    told to auto-commit. This module NEVER runs git in its first version (the genome -> scaffold
    application is not wired yet -- see TODO in run_iteration); auto_commit returns "commit_pending".

This module composes the already-built primitives READ-ONLY (frozen, guards, sentinel, archive, the
loop's validate). It does not re-implement any of them and does not edit the judge.

  python -m relay.selfimprove.test_l2     # hermetic tests (no real solve, no git, no network)
"""
from __future__ import annotations

import os
import sys
from typing import Callable, Iterable, Optional

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from relay.selfimprove import frozen as F
from relay.selfimprove import sentinel as S
from relay.selfimprove import experiment as EX
from relay.selfimprove.archive import Archive, genome_id
from relay.selfimprove.loop import validate as _real_validate


# --------------------------------------------------------------------------------------------------
# Spend ceiling (section 8: per-iteration / per-day budget, hard stop at the ceiling)
# --------------------------------------------------------------------------------------------------

class SpendCeiling:
    """Track iteration count and wall-clock since a fixed start, and report when a ceiling is hit.

    Time is NEVER read inside -- the caller passes `now_ts` (time.time()) into exceeded(), so the
    ceiling is deterministic and unit-testable. `start_ts` is the wall-clock the run began at.
    """

    def __init__(self, start_ts: float, iters: int = 0):
        self.start_ts = float(start_ts)
        self.iters = int(iters)

    def tick(self) -> int:
        """Count one completed iteration; return the new running total."""
        self.iters += 1
        return self.iters

    def elapsed_hours(self, now_ts: float) -> float:
        """Wall-clock hours since start (clamped to >= 0 so a clock skew never reads negative)."""
        return max(0.0, (float(now_ts) - self.start_ts) / 3600.0)

    def exceeded(self, max_iters: Optional[int], max_hours: Optional[float], now_ts: float) -> bool:
        """True iff the iteration count has reached max_iters OR elapsed wall-clock >= max_hours.

        Either bound may be None to disable it. The check is inclusive (>=): max_iters=5 means the
        ceiling is hit once 5 iterations have run, so the 6th must not start.
        """
        if max_iters is not None and self.iters >= int(max_iters):
            return True
        if max_hours is not None and self.elapsed_hours(now_ts) >= float(max_hours):
            return True
        return False


def run_until(stop_when: Callable[[], bool], iterate_fn: Callable[[], dict],
              max_steps: int = 1000) -> list:
    """Minimal driver: call iterate_fn() until stop_when() is True (or max_steps for safety).

    stop_when is checked BEFORE each step (so a ceiling already at its limit runs zero iterations).
    Returns the list of per-step results. The real cron wiring (CronCreate) lives a layer up; this is
    just the in-process helper so the spend ceiling can gate a sequence of iterations.
    """
    results: list = []
    steps = 0
    while not stop_when() and steps < max_steps:
        results.append(iterate_fn())
        steps += 1
    return results


# --------------------------------------------------------------------------------------------------
# The L2 iteration
# --------------------------------------------------------------------------------------------------

def _summarize_report(report: dict) -> dict:
    """A compact, JSON-safe summary of the loop's validate report (counts + plan, not the full gate)."""
    return {
        "toggle": report.get("toggle"),
        "n": report.get("n"),
        "dataset": report.get("dataset"),
        "on_resolved": report.get("on_resolved"),
        "off_resolved": report.get("off_resolved"),
    }


def run_iteration(*, toggle, n, dataset_key="Verified", auto_commit=False,
                  sentinel_path=None, archive_path=None, baseline_path=None,
                  on_resolved_ids=None, off_resolved_ids=None,
                  validate_fn: Callable[..., Optional[dict]] = _real_validate,
                  **validate_kwargs) -> dict:
    """Run ONE self-improvement iteration under L2 discipline and return a structured verdict dict.

    Steps (each is a tripwire; any failure short-circuits without keeping/committing):

      1. frozen check (before): frozen.frozen_intact(). If the judge changed -> status "abort".
      2. run the iteration: validate_fn(...) (the loop's validate by default; tests inject a stub).
         A None report (validate aborted internally) -> status "error".
      3. frozen RE-check (after): a long run could have been tampered mid-flight -> status
         "abort_post" and do NOT keep/commit.
      4. combine verdicts: gate = report["gate"], plus the cross-dataset sentinel. The candidate's
         per-id resolved set now comes from the report itself (report["on"]["resolved_ids"]);
         on_resolved_ids= still overrides it for tests. A sentinel that is CONFIGURED but cannot be
         evaluated FAILS CLOSED under auto_commit -- see below.
      5. record: append the tested genome to the Archive with pass_at_1, the REAL slice ids, and the
         experiment identity + harness fingerprint as descriptors.
      6. decide: final_keep = combined keep AND frozen intact (both checks). Then:
           - final_keep and auto_commit -> "commit_pending" (git wiring deferred; see TODO).
           - final_keep and not auto_commit -> "queued" (human review; the default-safe path).
           - not final_keep -> "rejected" with the reason.

    SENTINEL FAIL-CLOSED. A configured sentinel that cannot be evaluated is uncertainty, and
    uncertainty must not read as success: under auto_commit it forces NO APPLY rather than falling
    through to gate-only. This is the case the tripwire exists for -- a grader- or dataset-specific
    gain looks exactly like a gate win, and the sentinel is the only thing that would have caught it.
    Without auto_commit the run still queues for human review, carrying the unevaluable note, because
    discarding a completed measurement helps nobody.
    """
    baseline = baseline_path or F.DEFAULT_BASELINE
    notes: list[str] = []

    # ---- STEP 1: frozen check (before the run) ----------------------------------------------------
    ok_pre, changed_pre = F.frozen_intact(baseline_path=baseline)
    if not ok_pre:
        return {
            "status": "abort",
            "reason": "frozen set changed: %s" % ", ".join(changed_pre),
            "frozen_ok": False,
            "gate": None,
            "sentinel": None,
            "final_keep": False,
            "report": None,
        }

    # ---- STEP 2: run the iteration ---------------------------------------------------------------
    report = validate_fn(toggle=toggle, n=n, dataset_key=dataset_key, **validate_kwargs)
    if report is None:
        return {
            "status": "error",
            "reason": "validate returned None",
            "frozen_ok": True,
            "gate": None,
            "sentinel": None,
            "final_keep": False,
            "report": None,
        }

    # ---- STEP 3: frozen RE-check (after the run) -------------------------------------------------
    ok_post, changed_post = F.frozen_intact(baseline_path=baseline)
    if not ok_post:
        return {
            "status": "abort_post",
            "reason": "frozen set changed during run: %s" % ", ".join(changed_post),
            "frozen_ok": False,
            "gate": report.get("gate"),
            "sentinel": None,
            "final_keep": False,
            "report": _summarize_report(report),
        }

    gate = report.get("gate") or {}
    gate_keep = bool(gate.get("keep"))

    # ---- STEP 4: combine the gate verdict with the cross-dataset sentinel -------------------------
    sentinel_out = None
    combined_keep = gate_keep
    combined_reason = gate.get("reason", "")

    # The report now carries per-instance sets, so take the candidate's resolved ids from it
    # when the caller did not pass them explicitly. Before this, l2 could only be given them
    # by hand, which meant the sentinel was almost never actually evaluated.
    if on_resolved_ids is None:
        on_resolved_ids = ((report.get("on") or {}).get("resolved_ids"))
    if off_resolved_ids is None:
        off_resolved_ids = ((report.get("off") or {}).get("resolved_ids"))

    # "NO SENTINEL WAS ASKED FOR" AND "THE SENTINEL WE ASKED FOR IS GONE" ARE DIFFERENT FACTS.
    # Both used to land in the same branch -- not configured, gate-only -- so deleting
    # sentinel.json silently removed the tripwire and every subsequent run reported a clean
    # gate-only pass. A caller that named a path is a caller that wanted the canary checked;
    # its absence is the unevaluable case, not the unconfigured one.
    sentinel_requested = bool(sentinel_path)
    sentinel_configured = sentinel_requested and os.path.isfile(sentinel_path)
    sentinel_unevaluable = ""
    if sentinel_requested and not sentinel_configured:
        sentinel_unevaluable = "configured sentinel file is missing: %s" % sentinel_path
    elif sentinel_configured and on_resolved_ids is not None:
        sent = S.Sentinel(sentinel_path)
        if sent.members() and sent.baseline():
            sentinel_result = sent.check(on_resolved_ids)
            verdict = S.sentinel_verdict(gate_keep, sentinel_result)
            combined_keep = bool(verdict["keep"])
            combined_reason = verdict["reason"]
            sentinel_out = {**sentinel_result, "verdict_reason": verdict["reason"]}
        else:
            sentinel_unevaluable = "configured file has no members+baseline"
    elif sentinel_configured and on_resolved_ids is None:
        sentinel_unevaluable = "no candidate resolved-id set available"
    else:
        notes.append("sentinel skipped: not configured (gate-only)")

    # FAIL CLOSED. A sentinel that is configured but cannot be evaluated is UNCERTAINTY, and
    # uncertainty must not read as success. Previously this fell through to gate-only, so an
    # auto-apply run could keep a candidate whose tripwire was never checked -- precisely the
    # case the tripwire exists for, since a grader-specific gain shows up as a gate win.
    #
    # Only auto_commit is blocked. Without it the run is queued for human review anyway, and
    # a human looking at "sentinel: unevaluable" is the outcome we want, not a hard abort
    # that throws away a completed measurement.
    if sentinel_unevaluable:
        notes.append("sentinel UNEVALUABLE: %s" % sentinel_unevaluable)
        if auto_commit:
            combined_keep = False
            combined_reason = ("sentinel configured but unevaluable (%s); auto-apply requires "
                               "an evaluated sentinel" % sentinel_unevaluable)
            sentinel_out = {"status": "unevaluable", "reason": sentinel_unevaluable}

    # ---- STEP 5: record the tested genome in the archive -----------------------------------------
    # TODO: the real genome wiring (knobs/cards diff over the frozen base, parent selection from the
    # archive) is build-order #2. For now represent the tested change as the single toggle being ON.
    genome = {"knobs": {str(toggle): "1"}, "cards": {}, "parent_id": None, "note": "L2 iteration"}
    archive = Archive(archive_path) if archive_path else Archive()
    pass_at_1 = report.get("on_resolved")

    # Identity for this attempt. Without it a result cannot be cited: reports were named by
    # timestamp, archive rows keyed by genome hash, and nothing joined a solve log to the
    # row it produced. The fingerprint answers the other half -- WHICH harness ran -- so the
    # number can be reproduced rather than merely repeated.
    # TWO FINGERPRINTS, NOT ONE. The single fingerprint here was taken WITH the candidate
    # genome and then stored as `baseline_harness_id` while also serving as the parent for
    # `candidate_id` -- and `parent_harness_id` was left empty. Three fields describing the
    # same hash, two of them describing it wrongly, so a reader could not tell what was
    # compared with what. The baseline is the harness WITHOUT the genome; the candidate is
    # the harness WITH it; the parent of the candidate is the baseline.
    base_fp = EX.harness_fingerprint(genome={}, execution_profile=str(toggle or ""))
    cand_fp = EX.harness_fingerprint(genome=genome, execution_profile=str(toggle or ""))
    identity = EX.experiment_record(
        experiment_id=EX.new_experiment_id(),
        candidate_id_=EX.candidate_id(genome,
                                      parent_harness_id=base_fp["harness_id"]),
        parent_harness_id=base_fp["harness_id"],
        baseline_harness_id=base_fp["harness_id"],
        dataset=report.get("dataset", dataset_key),
        # The REAL slice, not []. Writing an empty list where the data exists was the
        # specific defect: it made every archived experiment unattributable to its tasks.
        slice_ids=report.get("slice_ids") or [],
        toggle=str(toggle or ""),
        seed=report.get("seed"),
    )
    archive.add(
        genome,
        slice_ids=identity["slice_ids"],
        pass_at_1=pass_at_1,
        gate_verdict=gate.get("verdict"),
        # Both fingerprints, with their fields, so a reader can see WHY the two differ
        # rather than being handed two opaque hashes and told they are not equal.
        descriptors={"experiment": identity,
                     "baseline_fingerprint": base_fp,
                     "candidate_fingerprint": cand_fp,
                     "candidate_harness_id": cand_fp["harness_id"]},
    )

    # ---- STEP 6: decide ---------------------------------------------------------------------------
    final_keep = bool(combined_keep) and ok_post

    result = {
        "frozen_ok": ok_post,
        "gate": gate,
        "sentinel": sentinel_out,
        "final_keep": final_keep,
        "report": _summarize_report(report),
        "notes": notes,
        "genome_id": genome_id(genome),
    }

    if not final_keep:
        result["status"] = "rejected"
        result["reason"] = combined_reason or "gate did not keep"
        return result

    if auto_commit:
        # The genome -> scaffold application is not wired yet, so there is nothing to git-commit in
        # this first version. Do NOT run git. Return commit_pending so the caller (or build-order #2)
        # knows the change cleared every gate and is ready to be applied + committed.
        result["status"] = "commit_pending"
        result["reason"] = ("cleared gate + sentinel + frozen; auto_commit requested but genome->"
                            "scaffold application is not wired (no git run in this version)")
        return result

    # default-safe: queue for human review
    result["status"] = "queued"
    result["reason"] = "cleared gate + sentinel + frozen; queued for human review (auto_commit=False)"
    return result
