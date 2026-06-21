"""Calibrated competence model -- the agent knowing how good it actually is, by task-class.

This is Bet #2 in bench/AGENT_STRENGTHS.md: a self-knowing, CALIBRATED agent. The measurement
harness already grades the agent's own attempts; this module reads that grade history (READ-ONLY)
and reports MEASURED pass@1 per task-class with a Wilson confidence interval. The payoff is routing:
spend cheap parallelism (best-of-N) where the agent is *measurably* weak, run single-shot where it is
*measurably* strong -- efficiency from self-knowledge rather than vibes.

Discipline borrowed from the rest of the loop (cf. guards.py):
  - Treat the grade ledger as read-only input that a live run may be APPENDING to: only ever read it,
    never write or lock, and degrade to an empty result on any malformed line / missing file.
  - EVALERR is an eval-host fault, not a competence signal (cf. project_swe_eval_host_confound), so it
    is excluded from the denominator entirely -- never counted against the agent.

The task-class here is the SWE-bench repo owner (the part before "__"). A finer sub-class (the
*miss-type* cluster -- wrong-layer, underfit, off-by-output, etc.) is a future refinement; the repo
owner is a deterministic, zero-dependency first cut that is already useful for routing.
"""
from __future__ import annotations

import json
import math
import os

# Default location of the grade ledger, under the repo root.
_REPO_ROOT = r"C:\Users\USER\companion-mcp"
_DEFAULT_GRADE_PATH = os.path.join(_REPO_ROOT, ".fleet", "swe", "grade_results.jsonl")


# --------------------------------------------------------------------------------------------------
# 1. Task-class
# --------------------------------------------------------------------------------------------------

def classify_instance(instance_id: str) -> str:
    """The task-class for a SWE-bench instance id -- the repo owner (part before "__").

      "django__django-12345"            -> "django"
      "psf__requests-1"                 -> "psf"
      "scikit-learn__scikit-learn-2"    -> "scikit-learn"

    If there is no "__", return the whole id; empty/None -> "unknown". Deterministic by design. A
    finer sub-class (miss-type cluster) is a future refinement -- see module docstring.
    """
    if not instance_id:
        return "unknown"
    iid = str(instance_id)
    if "__" in iid:
        return iid.split("__", 1)[0]
    return iid


# --------------------------------------------------------------------------------------------------
# 2. Wilson 95% confidence interval (in percent)
# --------------------------------------------------------------------------------------------------

def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion, returned in PERCENT (0..100).

    k = successes, n = trials. n == 0 -> (0.0, 0.0). stdlib math only; never raises for valid ints.
    """
    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = (z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)) / denom
    low = (center - margin) * 100.0
    high = (center + margin) * 100.0
    # clamp to [0, 100] against tiny float drift
    low = max(0.0, min(100.0, low))
    high = max(0.0, min(100.0, high))
    return (low, high)


# --------------------------------------------------------------------------------------------------
# 3. Calibration report
# --------------------------------------------------------------------------------------------------

def _iter_records(path: str):
    """Yield parsed JSON records from the grade ledger, skipping blank/garbage lines. Never raises.

    Read-only: opened for read with errors replaced; a live appender is never blocked or corrupted.
    """
    if not path or not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict) and rec.get("instance_id"):
                    yield rec
    except Exception:
        return


def _empty_overall() -> dict:
    return {"n": 0, "resolved": 0, "pass_at_1": None, "ci_low": 0.0, "ci_high": 0.0}


def calibration_report(grade_results_path: str | None = None, *, dedupe: str = "latest") -> dict:
    """Measured pass@1 per task-class from the grade ledger, with Wilson 95% CIs.

    dedupe:
      - "latest" (default): an instance graded multiple times (different runids/scaffolds) is counted
        ONCE, keeping its most recent record (by ts; ties broken by file order, last wins).
      - "none": count every record as a separate trial.

    Verdict handling:
      - "RESOLVED" -> resolved = 1; everything else -> 0.
      - "EVALERR" records are EXCLUDED from the denominator entirely. An eval-host fault is infra, not
        a competence signal (cf. project_swe_eval_host_confound / the infra-vs-real discipline), so it
        is never counted against the agent.

    Returns:
      {"by_class": {class: {"n", "resolved", "pass_at_1", "ci_low", "ci_high"}},
       "overall": {... same fields ...},
       "n_records_read": int, "n_evalerr_excluded": int}

    Defensive: missing/empty/garbage file -> {"by_class": {}, "overall": empty, n_records_read: 0,
    n_evalerr_excluded: 0}. Never raises.
    """
    path = grade_results_path or _DEFAULT_GRADE_PATH
    records = list(_iter_records(path))
    n_records_read = len(records)

    if dedupe == "latest":
        # Keep the most recent record per instance_id. ts ascending, ties -> later file position wins;
        # so iterating in file order and replacing when ts >= stored ts yields "latest, tie->last".
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

    n_evalerr_excluded = 0
    # cls -> [n, resolved]
    by: dict[str, list] = {}
    for rec in effective:
        verdict = str(rec.get("verdict", "")).strip().upper()
        if verdict == "EVALERR":
            n_evalerr_excluded += 1
            continue  # infra fault: out of the denominator entirely
        cls = classify_instance(rec["instance_id"])
        slot = by.setdefault(cls, [0, 0])
        slot[0] += 1
        if verdict == "RESOLVED":
            slot[1] += 1

    def _block(n: int, resolved: int) -> dict:
        low, high = _wilson(resolved, n)
        return {
            "n": n,
            "resolved": resolved,
            "pass_at_1": (resolved / n) if n else None,
            "ci_low": low,
            "ci_high": high,
        }

    by_class = {cls: _block(n, r) for cls, (n, r) in by.items()}
    tot_n = sum(n for n, _ in by.values())
    tot_r = sum(r for _, r in by.values())
    overall = _block(tot_n, tot_r) if tot_n else _empty_overall()

    return {
        "by_class": by_class,
        "overall": overall,
        "n_records_read": n_records_read,
        "n_evalerr_excluded": n_evalerr_excluded,
    }


# --------------------------------------------------------------------------------------------------
# 4. Competence lookup
# --------------------------------------------------------------------------------------------------

def competence(report: dict, task_class: str) -> dict | None:
    """The by_class block for a task-class, or None if the class has no measured history."""
    if not report:
        return None
    return report.get("by_class", {}).get(task_class)


# --------------------------------------------------------------------------------------------------
# 5. Effort routing -- Bet #2's payoff
# --------------------------------------------------------------------------------------------------

def recommend_effort(report: dict, task_class: str, *, low_threshold: float = 0.7,
                     min_n: int = 5) -> dict:
    """Recommend best-of-N vs single-shot for a task-class, from MEASURED pass@1.

    Returns {"task_class", "mode", "reason"}:
      - no measured history, or n < min_n  -> "best-of-N", "insufficient data, default to caution"
        (when we don't KNOW, fan out -- the safe default).
      - measured pass@1 < low_threshold    -> "best-of-N", "measured weak class (pass@1 X%, n=Y)".
      - else                               -> "single-shot", "measured strong class (pass@1 X%, n=Y)".

    This feeds the routing layer: spend cheap parallelism where the agent is measurably weak, save it
    where it is measurably strong.
    """
    comp = competence(report, task_class)
    if comp is None or comp.get("n", 0) < min_n or comp.get("pass_at_1") is None:
        return {
            "task_class": task_class,
            "mode": "best-of-N",
            "reason": "insufficient data, default to caution",
        }
    p = comp["pass_at_1"]
    n = comp["n"]
    if p < low_threshold:
        return {
            "task_class": task_class,
            "mode": "best-of-N",
            "reason": "measured weak class (pass@1 %.0f%%, n=%d)" % (p * 100.0, n),
        }
    return {
        "task_class": task_class,
        "mode": "single-shot",
        "reason": "measured strong class (pass@1 %.0f%%, n=%d)" % (p * 100.0, n),
    }


# --------------------------------------------------------------------------------------------------
# 6. Text rendering
# --------------------------------------------------------------------------------------------------

def render_text(report: dict) -> str:
    """A compact ASCII table of measured competence, sorted by n desc, plus the overall line.

    Never raises -- a malformed/empty report renders a single 'no grade history yet' line.
    """
    try:
        by_class = (report or {}).get("by_class", {}) or {}
        overall = (report or {}).get("overall", {}) or {}
        if not by_class and not overall.get("n"):
            return "no grade history yet"

        header = "%-18s %5s %8s  %s" % ("class", "n", "pass@1", "95% CI")
        sep = "-" * len(header)
        rows = [header, sep]

        def _fmt(name: str, block: dict) -> str:
            n = block.get("n", 0)
            p = block.get("pass_at_1")
            p_str = "%5.1f%%" % (p * 100.0) if p is not None else "  n/a "
            ci = "[%5.1f, %5.1f]" % (block.get("ci_low", 0.0), block.get("ci_high", 0.0))
            return "%-18s %5d %8s  %s" % (name[:18], n, p_str, ci)

        ordered = sorted(by_class.items(), key=lambda kv: (-kv[1].get("n", 0), kv[0]))
        for name, block in ordered:
            rows.append(_fmt(name, block))
        rows.append(sep)
        rows.append(_fmt("OVERALL", overall) if overall.get("n") else "OVERALL            (none)")
        meta = "records read: %d   evalerr excluded: %d" % (
            (report or {}).get("n_records_read", 0), (report or {}).get("n_evalerr_excluded", 0))
        rows.append(meta)
        return "\n".join(rows)
    except Exception:
        return "no grade history yet"


# --------------------------------------------------------------------------------------------------
# 7. CLI
# --------------------------------------------------------------------------------------------------

def _main() -> int:
    try:
        report = calibration_report()
    except Exception:
        print("no grade history yet")
        return 0
    print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
