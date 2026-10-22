# -*- coding: utf-8 -*-
"""Promotion gate for Evolution Lab candidates (slice F7).

WHAT DECISION THIS SERVES. Whether a lab candidate may be PROMOTED, judged from the evidence the
earlier slices produce: F1 layout isolation, F3 lineage, F4 bench bundle, F5 security regression,
F6 canary. The gate is an independent verdict, not a promoter: it returns allow / deny with the
reasons and nothing else. Promotion itself stays with the frozen promotion machinery
(relay/org/promotion.py and the frozen file set); this module never edits, writes, runs git,
starts a process, calls the fleet or promotes anything, and has no clock.

WHAT IT DOES. This module is pure. gate_verdict() takes one evidence object and returns a report
dict; to_json() prints it stably. Default off: no runtime module imports it, only
scripts/org_lab_promotion_gate.py, run by hand.

RULE. allow only if EVERY required input is present AND passing. Anything missing, failed,
malformed or ambiguous is a deny with a machine-readable reason. There is no partial allow and
there is no override: the gate takes no tolerance, minimum or switch from the evidence, so a caller
cannot loosen it. (The canary uses the defaults of relay/org/lab_canary.py.)

EVIDENCE (a JSON object; every key is required):
  candidate_id    the candidate asked about; a non-empty string.
  isolation       the report of lab_isolation.check_lab_layout (F1): "ok" true and no violations.
  lineage         the list of lab_lineage records (F3), or {"experiments": [...]}. The gate
                  recomputes check_lineage itself; the candidate must be one of the records and
                  its state must be "active" (a promoted, rejected or merged candidate is not
                  promoted again).
  bundle          a lab_bench_bundle bundle (F4). The gate recomputes check_bundle and the bundle
                  hash.
  security        the report of lab_security_regression.summarize (F5) built WITH coverage: "ok"
                  true, a "coverage" object present and no "not_frozen" entry in it.
  canary          {"candidate": RUN, "baseline": RUN, "security": report (optional)} (F6). The
                  gate recomputes shadow_verdict; both runs must name the hash of `bundle`.
                  The security report used there is the `security` input above.

REASONS (each is a stable code; deny if there is at least one). The reason a check exists is the
line under it.
  evidence_missing     a required key is absent or has the wrong type. Reason: silence is not
                       evidence; a gate that treats an absent check as passed is open by default.
  isolation_failed     the layout report is not ok, or lists a violation. Reason: a lab that shares
                       state with production cannot prove its results are its own.
  lineage_invalid      check_lineage found a violation, or the records are malformed. Reason: a
                       family tree with a loop or a cut chain makes "improved over its parent"
                       meaningless.
  candidate_unknown    the candidate is not in the lineage records. Reason: a candidate with no
                       recorded origin cannot be explained later.
  candidate_not_active the candidate's state is not "active". Reason: promoting a rejected or an
                       already promoted candidate repeats or reverses an earlier decision.
  bundle_invalid       check_bundle found a violation, or the bundle is malformed, or its recorded
                       hash is not the hash of its content. Reason: results are only comparable
                       when what was measured is pinned.
  bundle_mismatch      the canary runs do not name the hash of `bundle`. Reason: scores measured
                       under another setup say nothing about this bundle.
  security_failed      the security report is not ok, has no coverage, or coverage lists a
                       "not_frozen" file. Reason: dropping a file from the frozen set is how a
                       guard is switched off.
  canary_failed        the recomputed canary verdict is not pass (fail or insufficient), or the
                       runs are malformed. Reason: the candidate must be shown not worse than the
                       baseline on enough tasks.

No numeric threshold is defined here; the only numbers in force are those of lab_canary, with their
reasons in that module.
"""
from __future__ import annotations

import json

from relay.org import lab_bench_bundle as _bundle
from relay.org import lab_canary as _canary
from relay.org import lab_lineage as _lineage

VERSION = 1
ALLOW, DENY = "allow", "deny"
REQUIRED_KEYS = ("candidate_id", "isolation", "lineage", "bundle", "security", "canary")


def _records(lineage):
    if isinstance(lineage, dict):
        lineage = lineage.get("experiments")
    return lineage if isinstance(lineage, list) else None


def gate_verdict(evidence):
    """Report: {"version", "verdict", "allow", "candidate_id", "reasons": [{code, input, detail}]}.

    allow is true only when verdict is "allow" (no reason at all). A non-object `evidence` is a
    deny with evidence_missing for every key, not an exception: the gate never fails open.
    """
    reasons = []

    def deny(code, name, detail):
        reasons.append({"code": code, "input": name, "detail": detail})

    ev = evidence if isinstance(evidence, dict) else {}
    cid = ev.get("candidate_id")
    for key in REQUIRED_KEYS:
        if key not in ev or ev[key] is None:
            deny("evidence_missing", key, "required input %r is absent" % key)
    if "candidate_id" in ev and ev["candidate_id"] is not None \
            and (not isinstance(cid, str) or not cid.strip()):
        deny("evidence_missing", "candidate_id", "candidate_id is not a non-empty string")

    iso = ev.get("isolation")
    if iso is not None:
        if not isinstance(iso, dict) or iso.get("ok") is not True \
                or not isinstance(iso.get("violations"), list) or iso["violations"]:
            deny("isolation_failed", "isolation", "layout report is not ok or lists violations")

    records = None
    if ev.get("lineage") is not None:
        records = _records(ev["lineage"])
        if records is None:
            deny("lineage_invalid", "lineage", "lineage is not a list of records")
        else:
            try:
                rep = _lineage.check_lineage(records)
                if not rep["ok"]:
                    deny("lineage_invalid", "lineage", "%d lineage violation(s), first %s"
                         % (len(rep["violations"]), rep["violations"][0]["code"]))
                elif isinstance(cid, str):
                    mine = [r for r in records if isinstance(r, dict) and r.get("candidate_id") == cid]
                    if not mine:
                        deny("candidate_unknown", "lineage", "candidate %r is in no record" % cid)
                    elif any(r.get("state") != "active" for r in mine):
                        deny("candidate_not_active", "lineage",
                             "candidate %r has a record whose state is not 'active'" % cid)
            except _lineage.LabLineageError as exc:
                deny("lineage_invalid", "lineage", "records refused: %s" % exc)

    bundle = ev.get("bundle")
    bundle_hash = None
    if bundle is not None:
        try:
            rep = _bundle.check_bundle(bundle)
            bundle_hash = _bundle.compute_hash(bundle)
            if not rep["ok"]:
                deny("bundle_invalid", "bundle", "%d bundle violation(s), first %s"
                     % (len(rep["violations"]), rep["violations"][0]["code"]))
            elif bundle.get("bundle_hash") != bundle_hash:
                deny("bundle_invalid", "bundle", "recorded bundle_hash is not the hash of the content")
        except _bundle.LabBundleError as exc:
            deny("bundle_invalid", "bundle", "bundle refused: %s" % exc)

    sec = ev.get("security")
    if sec is not None:
        cov = sec.get("coverage") if isinstance(sec, dict) else None
        if not isinstance(sec, dict) or sec.get("ok") is not True or not isinstance(cov, dict) \
                or cov.get("not_frozen"):
            deny("security_failed", "security",
                 "security report is not ok, has no coverage, or lists a file dropped from the frozen set")

    can = ev.get("canary")
    if can is not None:
        if not isinstance(can, dict) or "candidate" not in can or "baseline" not in can:
            deny("canary_failed", "canary", "canary needs a candidate run and a baseline run")
        else:
            try:
                rep = _canary.shadow_verdict(can["candidate"], can["baseline"], sec)
                if not rep["ok"]:
                    deny("canary_failed", "canary", "shadow verdict is %s (%s)"
                         % (rep["verdict"], ", ".join(sorted({p["code"] for p in rep["problems"]}))))
                hashes = {can["candidate"]["bundle_hash"], can["baseline"]["bundle_hash"]}
                if bundle_hash is None or hashes != {bundle_hash}:
                    deny("bundle_mismatch", "canary", "canary runs do not name the hash of the bundle")
            except _canary.LabCanaryError as exc:
                deny("canary_failed", "canary", "runs refused: %s" % exc)

    return {"version": VERSION, "verdict": DENY if reasons else ALLOW, "allow": not reasons,
            "candidate_id": cid if isinstance(cid, str) else None, "reasons": reasons}


def to_json(obj):
    """Stable JSON text for a report."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, indent=2)
