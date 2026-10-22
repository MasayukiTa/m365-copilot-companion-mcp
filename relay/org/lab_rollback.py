# -*- coding: utf-8 -*-
"""Auto rollback decision for a promoted Evolution Lab candidate (slice F8).

WHAT DECISION THIS SERVES. After a candidate has been promoted, whether it should be rolled back,
judged from the promotion record and the shadow / canary results measured AFTER the promotion. The
module returns a PLAN and nothing else: "keep", "rollback" (with the exact tool call that undoes
it and why), or "undecided" (the evidence cannot support either). It never performs the rollback:
it does not call restore_point or roll_back, does not edit, write or delete anything, runs no git,
starts no process, calls no fleet and has no clock. A person or the frozen promotion machinery
runs the plan.

WHAT IT DOES. This module is pure. rollback_plan() takes one evidence object and returns a report
dict; to_json() prints it stably. Default off: no runtime module imports it, only
scripts/org_lab_rollback.py, run by hand.

EVIDENCE (a JSON object; every key is required).
  candidate_id   the promoted candidate; a non-empty string.
  promotion      the promotion record: {"candidate_id": same id, "bundle_hash": 64 hex,
                 "restore_point": the dict that tools/auto/autoloop.restore_point returned BEFORE
                 the promotion edited anything}. A usable restore point has ok true, and kind
                 "git" with a non-empty head, or kind "zip" with a non-empty snapshot.
  observations   a non-empty list of post-promotion shadow records, each
                 {"candidate": RUN, "baseline": RUN} in the shape of relay/org/lab_canary.py. The
                 baseline is the production run measured next to the promoted candidate. Every run
                 must name promotion["bundle_hash"].
  security       the report of lab_security_regression.summarize, read after the promotion.
The module recomputes lab_canary.shadow_verdict for every observation with that module's defaults.
It takes no tolerance, minimum or switch from the evidence, so the evidence cannot loosen it, and it
defines no number of its own.

DECISIONS (each reason is a stable code).
  rollback   at least one observation shows "regressed" (the promoted candidate is worse than the
             production baseline by more than the canary tolerance) or "security_not_ok" (the
             security regression report is absent or not ok). Reason: those are the two findings
             the shadow run exists to catch, and they are reported after the change is live. One
             such observation is enough because the plan is only a plan: acting on it is a
             separate, human-visible step, and keeping a worse candidate live costs more than
             reading one extra plan.
  keep       no observation is a finding, none is unusable, and at least one observation passes.
             Observations that are only "too_few_samples" are listed and do not count as a pass.
  undecided  anything else: evidence_missing, promotion_invalid, observation_malformed,
             observation_bundle_mismatch (a run names a bundle other than the promoted one),
             metrics_mismatch, or no observation that passes. Reason: silence and data that
             cannot be compared are neither a reason to undo a promotion nor a reason to confirm
             it; the report says what is missing. rollback wins over undecided: a regression
             that was measured is not cancelled by another observation that was unusable.

THE PLAN. For "rollback" the report has plan = {"steps": [...], "notes": [...]}. A usable git
restore point gives one step {"tool": "roll_back", "argument": the restore point exactly as
recorded}. tools/auto/autoloop.roll_back restores tracked files, LISTS untracked files and leaves
them alone, and refuses when HEAD is no longer the recorded head, so the notes say a promotion that
was committed needs that commit reverted first. A zip restore point gives no step: roll_back says a
snapshot has to be restored deliberately, so the plan names the snapshot and the note says to do it
by hand. An unusable or absent restore point gives no step and the code no_restore_point. The
restore point must have been taken BEFORE the promotion; this module cannot see when.
"""
from __future__ import annotations

import json
import re

from relay.org import lab_canary as _canary

VERSION = 1
KEEP, ROLLBACK, UNDECIDED = "keep", "rollback", "undecided"
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_KEYS = ("candidate_id", "promotion", "observations", "security")
# canary problem codes: the first group is a finding about the candidate, the second is data that
# cannot be compared.
FINDING_CODES = ("regressed", "security_not_ok")
DATA_CODES = ("bundle_mismatch", "metrics_mismatch")


def _restore_point_step(point):
    """(step or None, note or None, problem or None) for a recorded restore point."""
    if not isinstance(point, dict) or point.get("ok") is not True:
        return None, None, "restore point is absent or was not ok"
    kind = point.get("kind")
    if kind == "git":
        if not isinstance(point.get("head"), str) or not point["head"].strip():
            return None, None, "git restore point has no head"
        return ({"tool": "roll_back", "argument": point},
                "roll_back refuses if HEAD moved since head %s; if the promotion was committed, "
                "revert that commit first. Untracked files are listed and left alone."
                % point["head"][:12], None)
    if kind == "zip":
        if not isinstance(point.get("snapshot"), str) or not point["snapshot"].strip():
            return None, None, "zip restore point has no snapshot"
        return (None, "restore the snapshot %s by hand; roll_back does not unpack a snapshot"
                % point["snapshot"], None)
    return None, None, "restore point kind is neither git nor zip"


def rollback_plan(evidence):
    """Report: {"version", "decision", "candidate_id", "reasons": [{code, input, detail}],
    "observations": [{index, verdict, codes}], "plan": {"steps", "notes"} or None}.

    Never raises on bad evidence: a non-object is undecided with evidence_missing for every key.
    """
    reasons = []

    def add(code, name, detail):
        reasons.append({"code": code, "input": name, "detail": detail})

    ev = evidence if isinstance(evidence, dict) else {}
    cid = ev.get("candidate_id")
    for key in REQUIRED_KEYS:
        if key not in ev or ev[key] is None:
            add("evidence_missing", key, "required input %r is absent" % key)
    if ev.get("candidate_id") is not None and (not isinstance(cid, str) or not cid.strip()):
        add("evidence_missing", "candidate_id", "candidate_id is not a non-empty string")

    promo = ev.get("promotion")
    promo_hash, point = None, None
    if promo is not None:
        if not isinstance(promo, dict):
            add("promotion_invalid", "promotion", "promotion record is not an object")
        else:
            point = promo.get("restore_point")
            if promo.get("candidate_id") != cid:
                add("promotion_invalid", "promotion", "promotion record names another candidate")
            if not isinstance(promo.get("bundle_hash"), str) or not HASH_RE.match(promo["bundle_hash"]):
                add("promotion_invalid", "promotion", "bundle_hash is not 64 lowercase hex digits")
            else:
                promo_hash = promo["bundle_hash"]

    obs = ev.get("observations")
    sec = ev.get("security")
    summary = []
    findings = []          # (index, code, detail) that call for a rollback
    passes = 0
    if obs is not None:
        if not isinstance(obs, list) or not obs:
            add("evidence_missing", "observations", "observations is not a non-empty list")
        else:
            for i, o in enumerate(obs):
                if not isinstance(o, dict) or "candidate" not in o or "baseline" not in o:
                    add("observation_malformed", "observations[%d]" % i,
                        "needs a candidate run and a baseline run")
                    summary.append({"index": i, "verdict": "malformed", "codes": []})
                    continue
                try:
                    rep = _canary.shadow_verdict(o["candidate"], o["baseline"], sec)
                except _canary.LabCanaryError as exc:
                    add("observation_malformed", "observations[%d]" % i, "runs refused: %s" % exc)
                    summary.append({"index": i, "verdict": "malformed", "codes": []})
                    continue
                got = sorted({p["code"] for p in rep["problems"]})
                summary.append({"index": i, "verdict": rep["verdict"], "codes": got})
                hashes = {o["candidate"]["bundle_hash"], o["baseline"]["bundle_hash"]}
                if promo_hash is not None and hashes != {promo_hash}:
                    add("observation_bundle_mismatch", "observations[%d]" % i,
                        "runs do not name the bundle of the promotion record")
                    continue
                for p in rep["problems"]:
                    if p["code"] == "security_not_ok" and sec is None:
                        continue    # an absent report is evidence_missing (undecided), not a finding
                    if p["code"] in FINDING_CODES:
                        findings.append((i, p["code"], p["detail"]))
                    elif p["code"] in DATA_CODES:
                        add(p["code"], "observations[%d]" % i, p["detail"])
                if rep["ok"] and sec is not None:
                    passes += 1

    if findings:
        # a measured regression wins over unusable data; data reasons stay listed after it.
        reasons = [{"code": c, "input": "observations[%d]" % i, "detail": d}
                   for i, c, d in findings] + reasons
        step, note, problem = _restore_point_step(point)
        steps = [step] if step else []
        notes = [note] if note else []
        if problem or not steps and not notes:
            reasons.append({"code": "no_restore_point", "input": "promotion",
                            "detail": problem or "no usable restore point"})
            notes.append("no restore point can be used: the way back must be found by hand")
        return {"version": VERSION, "decision": ROLLBACK, "candidate_id": cid if isinstance(cid, str) else None,
                "reasons": reasons, "observations": summary,
                "plan": {"steps": steps, "notes": notes}}

    if not reasons and passes == 0:
        add("no_passing_observation", "observations",
            "no observation passes (all are too_few_samples), so keeping is not supported")
    decision = UNDECIDED if reasons else KEEP
    return {"version": VERSION, "decision": decision,
            "candidate_id": cid if isinstance(cid, str) else None,
            "reasons": reasons, "observations": summary, "plan": None}


def to_json(obj):
    """Stable JSON text for a report."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, indent=2)
