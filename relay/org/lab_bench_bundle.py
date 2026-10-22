# -*- coding: utf-8 -*-
"""Reproducible bench bundle for Evolution Lab experiments (slice F4).

WHAT DECISION THIS SERVES. Whether two benchmark results can be compared at all. A candidate is
"better than its parent" only if both were measured with the same tasks, the same evaluator, the
same model, the same seed and the same environment. Today those facts live in whoever started the
run. A bundle writes them down once, as a content-addressed record, so that a later reader (the
multi-objective archive, a promotion step) can check that a result belongs to the bundle it claims
and that a candidate run and a baseline run used one bundle. Nothing here decides promotion.

WHAT IT DOES. build_bundle() turns a spec into a bundle with a `bundle_hash` (sha256 of the
canonical JSON of everything else). check_bundle() returns a deterministic list of violations.
compare_bundles() says whether two bundles are comparable and lists every field that differs. It
is pure: it reads no file, writes no file, runs no git, starts no process, runs no benchmark and
has no clock (a bundle carries no timestamp on purpose: a time would make two identical setups
hash differently). Default off: nothing in production calls it, only scripts/org_lab_bench_bundle.py,
run by hand with --check.

BUNDLE: {"version", "experiment_id", "candidate_hash", "baseline_hash", "evaluator": {"id",
"version"}, "model": {"id"}, "seed", "pools": {pool: [task ids]}, "task_digests": {task id:
sha256 of the task content}, "environment": {"python", "platform"}, "bundle_hash"}. Pool names are
the ones of relay/org/genotype_bench.py (evolution / regression / sealed), so both modules share
one definition. Task order inside a pool does not matter: lists are sorted before hashing.

RULES (each is a stable code; the reason is the line under it).
  bundle_hash_format / bundle_hash_mismatch
      bundle_hash is not 64 lowercase hex digits, or does not equal the hash of the content.
      Reason: a bundle edited after it was recorded would still be cited by its old name.
  experiment_id_format
      experiment_id does not match the id shape of relay/org/lab_lineage.py. Reason: the bundle
      is joined to lineage records by this id.
  candidate_hash_format / baseline_hash_format
      not 64 lowercase hex digits (the full genotype digest, see lab_lineage.py). Reason: a
      short value cannot be told from a collision.
  unpinned_model
      model.id is empty or a floating alias (latest, default, auto, or ends with ":latest").
      Reason: a floating alias changes meaning between two runs of the "same" bundle.
  evaluator_unversioned
      evaluator.id or evaluator.version is empty. Reason: "the same evaluator" cannot be shown.
  seed_invalid
      seed is not an integer (a bool is not one). Reason: an unseeded run cannot be repeated.
  unknown_pool / missing_pool / empty_pool
      a pool name outside the known pools; the evolution or regression pool absent; a pool with no
      tasks. Reason: a bundle with nothing to measure cannot reproduce a measurement.
  duplicate_task
      a task id appears twice in one pool. Reason: it would be counted twice.
  pool_overlap
      a task id is in two pools. Reason: a task used for optimisation and also in the sealed or
      regression pool makes the held-out score a training score.
  task_digest_missing / task_digest_extra / task_digest_format
      a pooled task has no digest, a digest names a task in no pool, or a digest is not 64
      lowercase hex digits. Reason: without a content digest, an edited task keeps its id and
      two runs silently measure different things.
  environment_incomplete
      environment.python or environment.platform is empty. Reason: results differ across them.

compare_bundles() requires the fields that fix the measurement to be equal: evaluator, model,
seed, pools, task_digests, environment. experiment_id, candidate_hash and bundle_hash may differ
(different candidates run in one bundle share all of the first group). A bundle that fails
check_bundle is not comparable.

A record that is not an object, or a field of the wrong container type, raises LabBundleError
(bad input, not a finding).
"""
from __future__ import annotations

import hashlib
import json
import re

from relay.org import genotype_bench as _bench
from relay.org import lab_lineage as _lineage

VERSION = 1
REQUIRED_POOLS = (_bench.EVOLUTION, _bench.REGRESSION)
FLOATING_MODELS = ("latest", "default", "auto")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
# Fields that fix what is measured; equal in two bundles means their results are comparable.
MEASUREMENT_FIELDS = ("evaluator", "model", "seed", "pools", "task_digests", "environment")


class LabBundleError(ValueError):
    """An input is not a bundle-shaped object."""


def _obj(value, what):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise LabBundleError("%s must be an object" % what)
    return value


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _normalize(bundle):
    """Clean copy with a stable shape and sorted pools (not validated for content)."""
    if not isinstance(bundle, dict):
        raise LabBundleError("a bundle must be an object")
    pools_in = _obj(bundle.get("pools"), "pools")
    pools = {}
    for name, tasks in pools_in.items():
        if not isinstance(tasks, list) or not all(isinstance(t, str) for t in tasks):
            raise LabBundleError("pool %r must be a list of task id strings" % (name,))
        pools[str(name)] = sorted(tasks)
    digests = _obj(bundle.get("task_digests"), "task_digests")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in digests.items()):
        raise LabBundleError("task_digests must map task id strings to digest strings")
    evaluator = _obj(bundle.get("evaluator"), "evaluator")
    model = _obj(bundle.get("model"), "model")
    env = _obj(bundle.get("environment"), "environment")
    return {
        "version": VERSION,
        "experiment_id": _text(bundle.get("experiment_id")),
        "candidate_hash": _text(bundle.get("candidate_hash")),
        "baseline_hash": _text(bundle.get("baseline_hash")),
        "evaluator": {"id": _text(evaluator.get("id")),
                      "version": _text(evaluator.get("version"))},
        "model": {"id": _text(model.get("id"))},
        "seed": bundle.get("seed"),
        "pools": pools,
        "task_digests": dict(digests),
        "environment": {"python": _text(env.get("python")),
                        "platform": _text(env.get("platform"))},
    }


def compute_hash(bundle):
    """sha256 hex of the canonical JSON of the bundle without its own bundle_hash."""
    body = _normalize(bundle)
    text = json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def build_bundle(spec):
    """Normalised copy of `spec` with its bundle_hash filled in (any given hash is replaced)."""
    out = _normalize(spec)
    out["bundle_hash"] = compute_hash(out)
    return out


def check_bundle(bundle):
    """Deterministic report: {"version", "ok", "violations": [{code, id, detail}]}.

    `id` names what the finding is about (a field, a pool or a task id).
    """
    b = _normalize(bundle)
    v = []

    def add(code, rid, detail):
        v.append({"code": code, "id": rid, "detail": detail})

    given = _text(bundle.get("bundle_hash"))
    if not HASH_RE.match(given):
        add("bundle_hash_format", "bundle_hash", "bundle_hash is not 64 lowercase hex digits")
    elif given != compute_hash(b):
        add("bundle_hash_mismatch", "bundle_hash", "bundle_hash does not match the content")
    if not _lineage.EXPERIMENT_ID_RE.match(b["experiment_id"]):
        add("experiment_id_format", "experiment_id", "experiment_id %r does not match %s"
            % (b["experiment_id"], _lineage.EXPERIMENT_ID_RE.pattern))
    for key, code in (("candidate_hash", "candidate_hash_format"),
                      ("baseline_hash", "baseline_hash_format")):
        if not HASH_RE.match(b[key]):
            add(code, key, "%s is not 64 lowercase hex digits" % key)
    model = b["model"]["id"].lower()
    if not model or model in FLOATING_MODELS or model.endswith(":latest"):
        add("unpinned_model", "model.id", "model id %r is empty or a floating alias"
            % b["model"]["id"])
    if not b["evaluator"]["id"] or not b["evaluator"]["version"]:
        add("evaluator_unversioned", "evaluator", "evaluator id and version are both required")
    seed = b["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int):
        add("seed_invalid", "seed", "seed must be an integer")
    if not b["environment"]["python"] or not b["environment"]["platform"]:
        add("environment_incomplete", "environment",
            "environment.python and environment.platform are both required")

    pools = b["pools"]
    for name in sorted(pools):
        if name not in _bench.POOLS:
            add("unknown_pool", name, "pool %r is not one of %s" % (name, "/".join(_bench.POOLS)))
    for name in REQUIRED_POOLS:
        if name not in pools:
            add("missing_pool", name, "required pool %r is absent" % name)
    where = {}
    for name in sorted(pools):
        tasks = pools[name]
        if not tasks:
            add("empty_pool", name, "pool %r has no tasks" % name)
        for t in sorted(set(t for t in tasks if tasks.count(t) > 1)):
            add("duplicate_task", t, "task %r appears more than once in pool %r" % (t, name))
        for t in sorted(set(tasks)):
            where.setdefault(t, []).append(name)
    for t in sorted(where):
        if len(where[t]) > 1:
            add("pool_overlap", t, "task %r is in pools %s" % (t, ", ".join(where[t])))
    digests = b["task_digests"]
    for t in sorted(where):
        if t not in digests:
            add("task_digest_missing", t, "pooled task %r has no content digest" % t)
    for t in sorted(digests):
        if t not in where:
            add("task_digest_extra", t, "digest for %r, which is in no pool" % t)
        if not HASH_RE.match(digests[t]):
            add("task_digest_format", t, "digest of %r is not 64 lowercase hex digits" % t)
    return {"version": VERSION, "ok": not v, "violations": v}


def compare_bundles(a, b):
    """Whether results measured in bundle `a` and `b` can be compared.

    Returns {"version", "comparable", "differences": [field names], "invalid": [which side(s) failed
    check_bundle]}. Differences are listed for MEASUREMENT_FIELDS only; a bundle that fails its
    own check is never comparable.
    """
    na, nb = _normalize(a), _normalize(b)
    invalid = [name for name, x in (("a", a), ("b", b)) if not check_bundle(x)["ok"]]
    diffs = [f for f in MEASUREMENT_FIELDS if na[f] != nb[f]]
    return {"version": VERSION, "comparable": not diffs and not invalid,
            "differences": diffs, "invalid": invalid}


def to_json(obj):
    """Stable JSON text for a report or bundle."""
    return json.dumps(obj, ensure_ascii=True, sort_keys=True, indent=2)
