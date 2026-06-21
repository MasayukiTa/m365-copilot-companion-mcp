"""Unified self-improvement STATUS -- one operator view of "how is the agent doing, and what next".

Ties together the read-only views the controller already exposes into a single command:
  * the self-improvement SCORECARD   (dashboard.dashboard_state -> latest pass@1 / A/B / archive)
  * the measured COMPETENCE table     (calibration.calibration_report -> pass@1 per task-class + CI)
  * the next IMPROVEMENT TARGET        (targeting.improvement_plan -> weakest class + misses to diagnose)

All three read the same ledgers READ-ONLY (a live run may be appending), and each already degrades to a
clean "no data yet" line, so this aggregator never raises. It is the text twin of the WPF
SelfImproveDashboard window -- a `python -m relay.selfimprove.status` an operator (or a cron health
check) can run anywhere.
"""
from __future__ import annotations

from relay.selfimprove import calibration as _cal
from relay.selfimprove import dashboard as _dash
from relay.selfimprove import targeting as _tgt


def _target_text(plan: dict) -> str:
    """Render the improvement_plan() result as a couple of lines. Never raises."""
    try:
        target = (plan or {}).get("target")
        if not target:
            return "NEXT TARGET\n  (none) -- %s" % (plan or {}).get("note", "no data")
        lines = ["NEXT TARGET",
                 "  class    : %s" % target.get("task_class"),
                 "  pass@1   : %.1f%% (n=%d)  95%% CI [%.1f, %.1f]" % (
                     (target.get("pass_at_1") or 0.0) * 100.0, target.get("n", 0),
                     target.get("ci_low", 0.0), target.get("ci_high", 0.0)),
                 "  headroom : %.1f pp" % ((target.get("headroom") or 0.0) * 100.0),
                 "  misses   : %d real miss(es) to diagnose" % len((plan or {}).get("misses", [])),
                 "  note     : %s" % (plan or {}).get("note", "")]
        return "\n".join(lines)
    except Exception:
        return "NEXT TARGET\n  (unavailable)"


def status_text() -> str:
    """One combined operator view: scorecard + competence + next target. Never raises."""
    blocks = []
    try:
        blocks.append(_dash.render_text(_dash.dashboard_state()))
    except Exception:
        blocks.append("SELF-IMPROVEMENT SCORECARD\n  (unavailable)")
    try:
        blocks.append("MEASURED COMPETENCE\n" + _cal.render_text(_cal.calibration_report()))
    except Exception:
        blocks.append("MEASURED COMPETENCE\n  (unavailable)")
    try:
        blocks.append(_target_text(_tgt.improvement_plan()))
    except Exception:
        blocks.append("NEXT TARGET\n  (unavailable)")
    sep = "\n\n" + ("=" * 48) + "\n\n"
    return sep.join(blocks)


def _main() -> int:
    print(status_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
