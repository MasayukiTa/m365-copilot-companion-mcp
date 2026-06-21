"""Improvement targeter -- which measured WEAKNESS the self-improvement controller attacks next.

This connects Bet #2 (a calibrated agent -- calibration.py) to the controller's propose/A-B loop
(guards.py / Bet #3). Calibration tells the agent how good it actually is per task-class; this module
reads that report, picks the WEAKEST well-sampled class with headroom, and assembles that class's real
misses so the controller has concrete diagnosis material -- instead of attacking weaknesses at random.

CRITICAL discipline (domain-general -- mirrors guards.overfit_lint):
  The task-class (SWE-bench repo owner) is ONLY a ROUTER / targeting signal -- it decides WHICH misses
  to look at. It must NEVER leak into the improvement itself. The card the controller proposes after
  diagnosing these misses must describe a failure MODE (wrong-layer, underfit, off-by-output, ...),
  never "a fix for django". overfit_lint() rejects any proposal that names a concrete repo/instance/
  file/test. So this module's output is "here are the misses to diagnose", not a repo-specific fix:
  the targeting is class-aware, the *lesson* stays class-agnostic.

Read-only, like calibration: the grade ledger may be APPENDED to by a live run; we only ever read it
and degrade to empty/None on any malformed line or missing file. stdlib only; deterministic.
"""
from __future__ import annotations

from relay.selfimprove import calibration as C


# --------------------------------------------------------------------------------------------------
# 1. Pick the next target -- the weakest well-sampled class with headroom
# --------------------------------------------------------------------------------------------------

def next_target(report: dict, *, min_n: int = 5, max_pass: float = 0.9,
                exclude=None) -> dict | None:
    """Choose which weakness to attack next, from a calibration_report() dict.

    A class QUALIFIES iff:
      - n >= min_n            (enough evidence to trust the rate -- not data-poor)
      - pass_at_1 is not None
      - pass_at_1 < max_pass  (has real headroom -- not already strong)
      - task_class not in (exclude or set())   (e.g. already burned / in-flight targets)

    Among qualifiers pick the WEAKEST = lowest pass_at_1; tie-break by larger n (more evidence),
    then class name (determinism).

    Returns {"task_class","pass_at_1","n","ci_low","ci_high","headroom","reason"} or None when
    nothing qualifies (everything already strong, or all data-poor/excluded).

    Defensive: empty / garbage / non-dict report -> None, never raises.
    """
    try:
        by_class = (report or {}).get("by_class", {}) or {}
        if not isinstance(by_class, dict):
            return None
        ex = set(exclude or ())

        candidates = []
        for cls, block in by_class.items():
            if cls in ex:
                continue
            if not isinstance(block, dict):
                continue
            n = block.get("n", 0)
            p = block.get("pass_at_1")
            if not isinstance(n, int) or n < min_n:
                continue
            if p is None or not isinstance(p, (int, float)):
                continue
            if p >= max_pass:
                continue
            candidates.append((cls, block, float(p), int(n)))

        if not candidates:
            return None

        # weakest first: lowest pass_at_1, then larger n, then class name.
        candidates.sort(key=lambda t: (t[2], -t[3], t[0]))
        cls, block, p, n = candidates[0]
        headroom = max_pass - p
        return {
            "task_class": cls,
            "pass_at_1": p,
            "n": n,
            "ci_low": block.get("ci_low", 0.0),
            "ci_high": block.get("ci_high", 0.0),
            "headroom": headroom,
            "reason": ("weakest well-sampled class with headroom: pass@1 %.0f%% (n=%d), "
                       "%.0f pp below max_pass %.0f%%"
                       % (p * 100.0, n, headroom * 100.0, max_pass * 100.0)),
        }
    except Exception:
        return None


# --------------------------------------------------------------------------------------------------
# 2. Assemble the real misses for a class -- the diagnosis material
# --------------------------------------------------------------------------------------------------

def assemble_misses(task_class: str, grade_results_path: str | None = None,
                    *, dedupe: str = "latest") -> list[str]:
    """Instance ids of the REAL misses in `task_class`, read from the grade ledger (READ-ONLY).

    Same reading discipline as calibration: default path = .fleet/swe/grade_results.jsonl under the
    repo root; dedupe="latest" keeps one record per instance_id (most recent ts), identical rule to
    calibration_report.

    An instance is included iff:
      - classify_instance(instance_id) == task_class, AND
      - its latest verdict is a REAL miss: NOT "RESOLVED" and NOT "EVALERR".
        EVALERR is an eval-host / infra fault (cf. project_swe_eval_host_confound), never a diagnosis
        target -- so it is excluded here exactly as it is excluded from calibration's denominator.

    Returns a sorted, deterministic list. These ids are what the controller feeds to its
    miss-analysis -> propose (a DOMAIN-GENERAL card) -> frozen A/B gate.

    Defensive: missing file / no misses -> [].
    """
    try:
        path = grade_results_path or C._DEFAULT_GRADE_PATH
        records = list(C._iter_records(path))

        if dedupe == "latest":
            chosen: dict[str, dict] = {}
            for rec in records:
                iid = rec["instance_id"]
                ts = rec.get("ts")
                ts_val = ts if isinstance(ts, (int, float)) else float("-inf")
                prev = chosen.get(iid)
                if prev is None:
                    chosen[iid] = rec
                else:
                    prev_ts = prev.get("ts")
                    prev_val = prev_ts if isinstance(prev_ts, (int, float)) else float("-inf")
                    if ts_val >= prev_val:
                        chosen[iid] = rec
            effective = list(chosen.values())
        else:
            effective = records

        misses = set()
        for rec in effective:
            iid = rec["instance_id"]
            if C.classify_instance(iid) != task_class:
                continue
            verdict = str(rec.get("verdict", "")).strip().upper()
            if verdict == "RESOLVED" or verdict == "EVALERR":
                continue  # resolved = not a miss; EVALERR = infra, never a diagnosis target
            misses.add(iid)
        return sorted(misses)
    except Exception:
        return []


# --------------------------------------------------------------------------------------------------
# 3. Convenience: full improvement plan (target + misses + a domain-general reminder)
# --------------------------------------------------------------------------------------------------

_NOTE_NO_TARGET = ("no weak class with enough data; either fan out best-of-N on data-poor classes "
                   "or gather more graded attempts")
_NOTE_HAVE_TARGET = ("diagnose these misses for a DOMAIN-GENERAL failure-mode card; do NOT propose a "
                     "repo-specific fix (overfit_lint will reject it)")


def improvement_plan(report: dict | None = None, *, min_n: int = 5, max_pass: float = 0.9,
                     exclude=None, grade_results_path: str | None = None) -> dict:
    """Pick the next target and assemble its misses in one call.

    If `report` is None, builds it via calibration.calibration_report(grade_results_path).

    Returns:
      - no qualifying target -> {"target": None, "misses": [], "note": <no-target note>}
      - otherwise            -> {"target": <next_target dict>,
                                  "misses": <assemble_misses for that class>,
                                  "note": <domain-general reminder>}

    The note is the discipline made explicit: the target class only ROUTES diagnosis; the proposed
    fix must stay domain-general or the overfit linter rejects it.

    Defensive: never raises.
    """
    try:
        if report is None:
            report = C.calibration_report(grade_results_path)
        target = next_target(report, min_n=min_n, max_pass=max_pass, exclude=exclude)
        if target is None:
            return {"target": None, "misses": [], "note": _NOTE_NO_TARGET}
        misses = assemble_misses(target["task_class"], grade_results_path)
        return {"target": target, "misses": misses, "note": _NOTE_HAVE_TARGET}
    except Exception:
        return {"target": None, "misses": [], "note": _NOTE_NO_TARGET}


# --------------------------------------------------------------------------------------------------
# 4. CLI
# --------------------------------------------------------------------------------------------------

def _main() -> int:
    try:
        plan = improvement_plan()
    except Exception:
        print("no grade history yet")
        return 0

    target = plan.get("target")
    if not target:
        print("next target: (none) -- %s" % plan.get("note", ""))
        return 0

    print("next target: %s" % target["task_class"])
    print("  pass@1   : %.1f%% (n=%d)  95%% CI [%.1f, %.1f]"
          % (target["pass_at_1"] * 100.0, target["n"],
             target.get("ci_low", 0.0), target.get("ci_high", 0.0)))
    print("  headroom : %.1f pp   reason: %s"
          % (target["headroom"] * 100.0, target["reason"]))
    print("  misses   : %d real miss(es) to diagnose" % len(plan.get("misses", [])))
    print("  note     : %s" % plan.get("note", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
