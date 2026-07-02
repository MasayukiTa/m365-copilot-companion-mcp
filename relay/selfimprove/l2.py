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
      4. combine verdicts: gate = report["gate"]. If a sentinel is configured AND the candidate's
         per-id resolved set is available, run sentinel.check() + sentinel_verdict(); otherwise fall
         back to gate-only with a logged note (the sentinel is SKIPPED, never fabricated -- loop's
         validate does not currently expose per-id resolved sets, only counts).
      5. record: append the tested genome to the Archive with pass_at_1 from the report.
      6. decide: final_keep = combined keep AND frozen intact (both checks). Then:
           - final_keep and auto_commit -> "commit_pending" (git wiring deferred; see TODO).
           - final_keep and not auto_commit -> "queued" (human review; the default-safe path).
           - not final_keep -> "rejected" with the reason.

    The candidate resolved set for the sentinel is the ON-arm resolved ids. loop.validate does not
    expose per-id sets today, so pass `on_resolved_ids=` (and optionally off) to enable the sentinel;
    omit them to fall back to gate-only.
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

    sentinel_configured = bool(sentinel_path) and os.path.isfile(sentinel_path)
    if sentinel_configured and on_resolved_ids is not None:
        sent = S.Sentinel(sentinel_path)
        if sent.members() and sent.baseline():
            sentinel_result = sent.check(on_resolved_ids)
            verdict = S.sentinel_verdict(gate_keep, sentinel_result)
            combined_keep = bool(verdict["keep"])
            combined_reason = verdict["reason"]
            sentinel_out = {**sentinel_result, "verdict_reason": verdict["reason"]}
        else:
            notes.append("sentinel skipped: configured file has no members+baseline")
    elif sentinel_configured and on_resolved_ids is None:
        # The sentinel exists but loop.validate did not hand us a per-id resolved set; do NOT
        # fabricate one -- fall back to gate-only and say so.
        notes.append("sentinel skipped: no candidate resolved-id set exposed by validate (gate-only)")
    else:
        notes.append("sentinel skipped: not configured (gate-only)")

    # ---- STEP 5: record the tested genome in the archive -----------------------------------------
    # TODO: the real genome wiring (knobs/cards diff over the frozen base, parent selection from the
    # archive) is build-order #2. For now represent the tested change as the single toggle being ON.
    genome = {"knobs": {str(toggle): "1"}, "cards": {}, "parent_id": None, "note": "L2 iteration"}
    archive = Archive(archive_path) if archive_path else Archive()
    pass_at_1 = report.get("on_resolved")
    archive.add(
        genome,
        slice_ids=[],                       # per-id slice not exposed by validate yet (TODO with #2)
        pass_at_1=pass_at_1,
        gate_verdict=gate.get("verdict"),
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
