# -*- coding: utf-8 -*-
"""Canary / shadow evaluator for Evolution Lab candidates (slice F6).

WHAT DECISION THIS SERVES. Whether a candidate, run in shadow next to production, is "not worse than
the baseline" well enough to be shown to the promotion step. A shadow run measures the candidate on
the same tasks as the production baseline, without the candidate touching production state. This
module only reads the two result sets and a security regression report and says pass / fail /
insufficient with the reasons. Nothing here decides promotion (that stays with the frozen promotion
machinery) and nothing here changes anything.

WHAT IT DOES. This module is pure: it reads no file, writes no file, runs no git, starts no process,
calls no fleet and has no clock. shadow_verdict() returns a report dict; to_json() prints it stably.
Default off: no runtime module imports it, only scripts/org_lab_canary.py, run by hand.

RUN (one per side): {"bundle_hash": 64 hex, "scores": {task_id: {metric: number}}}. Every metric is
"higher is better" and lies in [0, 1] (a rate or a score; a lower-is-better metric must be sent as
1 - value). bundle_hash is the one of relay/org/lab_bench_bundle.py.
SECURITY: the report of scripts/org_lab_security_regression.py (relay/org/lab_security_regression.py
summarize()); only its boolean "ok" is read.

CONSTANTS (each with what it prevents, and what was measured or why no measurement exists).
  DEFAULT_TOLERANCE = 0.02
      A metric of the candidate may be this much lower than the baseline's mean (absolute, on the
      [0, 1] scale) and still pass. It prevents a verdict that flips on measurement noise: two runs
      of one setup differ slightly, and a zero tolerance would fail an identical candidate half the
      time. It is NOT a measured noise level: no repeated-run record of this bench exists in the
      repository yet, so the value is a starting point that the caller overrides with
      --tolerance once such a record exists. A tolerance outside [0, 0.5] is refused: above 0.5 a
      candidate that loses most of the score would pass.
  DEFAULT_MIN_SAMPLES = 30
      Smallest number of common tasks. It prevents a verdict from a handful of tasks: with fewer
      than about 30 binary outcomes the standard error of a rate is above 0.09, larger than the
      default tolerance, so "not worse" could not be told from chance. 30 is the usual rule-of-thumb
      size at which that error stops dominating; it is not tuned to this bench. A value below 1 is
      refused.

RULES (each is a stable code; verdict "fail" if any of the first group, "insufficient" if only the
second group, otherwise "pass").
  fail:
    bundle_mismatch         the two runs name different bundle_hash values. Reason: results measured
                            with different tasks, evaluator, model or seed cannot be compared.
    metrics_mismatch        the sides have no metric in common, or a metric of the baseline is
                            missing in a common task of the candidate. Reason: a candidate must not
                            pass by leaving out the metric it is worse at.
    regressed               on a metric the candidate mean is lower than the baseline mean by more
                            than the tolerance. Reason: that is the thing the shadow run exists to
                            catch.
    security_not_ok         the security report is absent, not an object, or its "ok" is not true.
                            Reason: a candidate that softened a frozen guard can look better.
  insufficient:
    too_few_samples         fewer common tasks than the minimum. Reason: see DEFAULT_MIN_SAMPLES.
Tasks present on only one side are listed (only_candidate / only_baseline) and are not scored:
the means use the common tasks only, so that both sides are averaged over the same tasks.

A run that is not an object, a score that is not a finite number in [0, 1] (a bool is not a number),
or a bad tolerance / minimum raises LabCanaryError (bad input, not a finding).
"""
from __future__ import annotations

import json
import math
import re

VERSION = 1
PASS, FAIL, INSUFFICIENT = "pass", "fail", "insufficient"
DEFAULT_TOLERANCE = 0.02
MAX_TOLERANCE = 0.5
DEFAULT_MIN_SAMPLES = 30
HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class LabCanaryError(ValueError):
    """An input is not the shape this module expects."""


def _check_run(run, what):
    if not isinstance(run, dict):
        raise LabCanaryError("%s run must be an object" % what)
    h = run.get("bundle_hash")
    if not isinstance(h, str) or not HASH_RE.match(h):
        raise LabCanaryError("%s bundle_hash is not 64 lowercase hex digits" % what)
    scores = run.get("scores")
    if not isinstance(scores, dict):
        raise LabCanaryError("%s scores must be an object of task -> metrics" % what)
    for task, metrics in scores.items():
        if not isinstance(task, str) or not isinstance(metrics, dict):
            raise LabCanaryError("%s score for %r must be an object of metric -> number" % (what, task))
        for name, value in metrics.items():
            if (not isinstance(name, str) or isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 <= value <= 1):
                raise LabCanaryError("%s score %r/%r is not a number in [0, 1]" % (what, task, name))
    return scores


def codes_of(problems):
    return [p["code"] for p in problems]


def _mean(values):
    return math.fsum(values) / len(values)


def shadow_verdict(candidate, baseline, security, tolerance=DEFAULT_TOLERANCE,
                   min_samples=DEFAULT_MIN_SAMPLES):
    """Report: {"version", "verdict", "ok", "samples", "tolerance", "min_samples", "metrics",
    "problems": [{code, id, detail}], "only_candidate", "only_baseline"}. ok means verdict pass."""
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) \
            or not math.isfinite(tolerance) or not 0 <= tolerance <= MAX_TOLERANCE:
        raise LabCanaryError("tolerance must be a number in [0, %s]" % MAX_TOLERANCE)
    if isinstance(min_samples, bool) or not isinstance(min_samples, int) or min_samples < 1:
        raise LabCanaryError("min_samples must be an integer of at least 1")
    cs = _check_run(candidate, "candidate")
    bs = _check_run(baseline, "baseline")
    problems = []

    def add(code, rid, detail):
        problems.append({"code": code, "id": rid, "detail": detail})

    if candidate["bundle_hash"] != baseline["bundle_hash"]:
        add("bundle_mismatch", "bundle_hash", "candidate and baseline name different bundles")
    common = sorted(set(cs) & set(bs))
    only_c, only_b = sorted(set(cs) - set(bs)), sorted(set(bs) - set(cs))

    base_metrics = sorted({m for t in common for m in bs[t]})
    metrics = {}
    for m in base_metrics:
        lacking = [t for t in common if m in bs[t] and m not in cs[t]]
        if lacking:
            add("metrics_mismatch", m, "candidate lacks metric %r on %d common task(s), first %s"
                % (m, len(lacking), lacking[0]))
            continue
        tasks = [t for t in common if m in bs[t]]
        cm, bm = _mean([cs[t][m] for t in tasks]), _mean([bs[t][m] for t in tasks])
        delta = cm - bm
        # tolerance is compared with a tiny slack so that a delta of exactly -tolerance written in
        # floating point (0.8 - 0.82 = -0.01999...) is not failed by rounding alone.
        regressed = delta < -tolerance - 1e-12
        metrics[m] = {"candidate": cm, "baseline": bm, "delta": delta, "n": len(tasks),
                      "regressed": regressed}
        if regressed:
            add("regressed", m, "%s is %.4f lower than the baseline (tolerance %s)"
                % (m, -delta, tolerance))
    if not metrics and "metrics_mismatch" not in codes_of(problems):
        add("metrics_mismatch", "metrics", "there is no metric to compare on the common tasks")
    if not isinstance(security, dict) or security.get("ok") is not True:
        add("security_not_ok", "security", "the security regression report is absent or not ok")

    failing = [p for p in problems]
    insufficient = len(common) < min_samples
    if insufficient:
        add("too_few_samples", "samples", "%d common task(s), at least %d needed"
            % (len(common), min_samples))
    verdict = FAIL if failing else (INSUFFICIENT if insufficient else PASS)
    return {"version": VERSION, "verdict": verdict, "ok": verdict == PASS, "samples": len(common),
            "tolerance": tolerance, "min_samples": min_samples, "metrics": metrics,
            "problems": problems, "only_candidate": only_c, "only_baseline": only_b}


def to_json(obj):
    """Stable JSON text for a report."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, indent=2)
