"""Identity and fingerprint for one self-improvement experiment.

Two things were missing from every experiment this repository has ever run, and without
them a result cannot be attributed to a change:

  * an ID. Reports were named by timestamp, the archive keyed entries by genome hash, and
    nothing tied a solve log to the grade log to the archive row that came out of them. Two
    runs of the same toggle were indistinguishable after the fact.
  * a fingerprint of the harness that actually ran. "SWE_MISS85_DISCIPLINE=1" says which
    toggle moved; it says nothing about the commit, the other toggles, the model, or the
    execution profile the arm ran under -- so a number could not be reproduced, only
    repeated and hoped for.

Both are pure description. Nothing here decides, gates, or grades: an experiment that
cannot be identified is still evaluated exactly as before, it just cannot be cited. That
separation is deliberate -- see the brief's rule that the LLM proposes and deterministic
code judges; this module is on neither side, it is the label on the jar.

Stdlib only, no I/O beyond an optional `git rev-parse`, and every function returns
something usable when the environment cannot answer (an unknown commit is recorded as
"unknown", never guessed and never omitted).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Toggles that change what the harness DOES. Recorded in the fingerprint so an arm run with
# an unrelated flag left on is not silently compared against one without it. Extend this
# list when a new behavioural toggle is added -- an unrecorded toggle is an unrecorded
# confound, which is exactly what this module exists to prevent.
FINGERPRINT_ENV_KEYS = (
    "SWE_MISS85_DISCIPLINE",
    "MCP_EXECUTION_PROFILE",
    "MCP_REPLY_SETTLE_SAMPLES",
    "MCP_REPLY_SETTLE_INTERVAL_S",
    "MCP_CONSENT_CHAIN_MAX",
    "MCP_SETTLE_TRACE_AFTER_S",
    # A markerless tail multiplies both the sample count and the dwell. It was a bare `* 2`
    # in several places, which is how two of them came to disagree about whether the sample
    # count was doubled at all; making it a constant also made it settable, and a settable
    # thing that changes when a reply is accepted belongs here.
    "MCP_MARKERLESS_DWELL_FACTOR",
    # Which settle implementation the migrated-but-gated sites used. Comparing a run
    # with it on against one without it is comparing two different harnesses, which is
    # the entire point of the A/B it exists for.
    "MCP_SETTLE_UNIFIED",
    # Whether run_episode asked the solver a follow-up question after grading (Phase 6,
    # relay.selfimprove.solver_feedback). The follow-up cannot change a grade -- it is asked
    # only after grade_final_state has already run -- but it is an extra turn, and a run made
    # with it on is not the same measurement as one made without it.
    "MCP_SOLVER_FEEDBACK",
    # Whether a run recorded lens verdicts into the refuter memory. It does not change
    # which lenses ran, but it changes what the ADAPTIVE policy will choose on every
    # later run -- so two runs made either side of it are not the same harness.
    "MCP_REFUTER_MEMORY_RECORD",
    "MCP_ADAPTIVE_REFUTER",
)


def _judge_digest() -> str:
    """One hash over the current contents of the frozen set, or "unavailable".

    Deliberately the FROZEN files rather than the whole tree: hashing everything would make
    the fingerprint change on an unrelated edit and nobody would trust it, while hashing
    nothing leaves git as the only witness -- and git is frequently not available here. The
    frozen set is exactly the code whose change invalidates a comparison.
    """
    try:
        from relay.selfimprove import frozen as _F
        sums = _F.compute_checksums()
        return hashlib.sha256(canonical(sums).encode("utf-8")).hexdigest()[:32]
    except Exception:
        return "unavailable"


def _git_commit() -> str:
    """The exact commit the harness ran from, or "unknown".

    Not raising matters: a fingerprint that refuses to exist because git is unavailable
    would make the experiment unrecordable, which is strictly worse than an honest
    "unknown" the reader can see.
    """
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO,
                             capture_output=True, text=True, timeout=15)
        if out.returncode == 0:
            return (out.stdout or "").strip() or "unknown"
    except Exception:
        pass
    return "unknown"


def _git_dirty() -> bool | None:
    """True if the working tree has uncommitted changes; None if it cannot be determined.

    A dirty tree means the commit alone does NOT identify the code that ran, so an
    experiment recorded as reproducible would not be. Worth one subprocess.
    """
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=REPO,
                             capture_output=True, text=True, timeout=15)
        if out.returncode == 0:
            return bool((out.stdout or "").strip())
    except Exception:
        pass
    return None


def canonical(obj) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace.

    The hash is only stable if the serialisation is, and dict ordering is not something to
    rely on across runs or interpreters.
    """
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def harness_fingerprint(*, genome=None, component_versions=None, execution_profile="",
                        model="", env=None, extra=None) -> dict:
    """Describe the harness that is about to run, and hash that description.

    Returns {"harness_id": sha256-hex, "fields": {...}} -- the id for citing, the fields so
    a human can see WHY two ids differ. A bare hash that nobody can explain is not
    reproducibility, it is a checksum.
    """
    source = env if env is not None else os.environ
    fields = {
        "git_commit": _git_commit(),
        "git_dirty": _git_dirty(),
        "genome": genome or {},
        "component_versions": component_versions or {},
        "execution_profile": execution_profile or source.get("MCP_EXECUTION_PROFILE", ""),
        "model": model,
        "env_toggles": {k: source.get(k) for k in FINGERPRINT_ENV_KEYS if source.get(k) is not None},
        # THE COMMIT IS NOT ENOUGH, and on this machine it is often not even available: git
        # off PATH makes git_commit "unknown" for every run, and a dirty tree records only
        # git_dirty=True, so two harnesses with completely different uncommitted changes
        # fingerprint identically. That turns the id from "which harness ran" into "which
        # harness roughly ran", which is the half that cannot be cited. Hashing the judge's
        # own files pins the part that decides outcomes, independently of git.
        "judge_digest": _judge_digest(),
    }
    if extra:
        fields["extra"] = extra
    return {
        "harness_id": hashlib.sha256(canonical(fields).encode("utf-8")).hexdigest(),
        "fields": fields,
    }


def new_experiment_id(prefix="exp", ts=None) -> str:
    """A stable unique id for one self-improvement attempt.

    Time-ordered on purpose: experiment ids end up in filenames and log greps, and an id
    that sorts chronologically is worth more day to day than one with more entropy. The
    random tail only has to separate two attempts started in the same second.
    """
    when = time.time() if ts is None else ts
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(when))
    tail = hashlib.sha256(("%s|%s" % (when, os.getpid())).encode("utf-8")).hexdigest()[:6]
    return "%s-%s-%s" % (prefix, stamp, tail)


def candidate_id(genome, *, parent_harness_id="") -> str:
    """Identify the CANDIDATE, not the run.

    Derived from the genome plus its parent, so the same genome proposed from a different
    parent is a different candidate -- which it is: the change under test is the edge, not
    the node. Re-running the identical candidate reuses the id, which is what makes
    repeated attempts groupable.
    """
    return hashlib.sha256(
        canonical({"genome": genome or {}, "parent": parent_harness_id}).encode("utf-8")
    ).hexdigest()[:16]


def experiment_record(*, experiment_id, candidate_id_, parent_harness_id,
                      baseline_harness_id, dataset, slice_ids, toggle="",
                      grader_version="", security_policy_version="", seed=None) -> dict:
    """The identity block carried by every artefact of one experiment.

    Kept in ONE place and copied verbatim into the report, the archive entry and the logs,
    so those three can be joined afterwards. Every field here answers a question that came
    up while reading an old result and could not be answered.
    """
    return {
        "experiment_id": experiment_id,
        "candidate_id": candidate_id_,
        "parent_harness_id": parent_harness_id,
        "baseline_harness_id": baseline_harness_id,
        "dataset": dataset,
        "slice_ids": list(slice_ids or []),
        "toggle": toggle,
        "grader_version": grader_version,
        "security_policy_version": security_policy_version,
        "seed": seed,
        "recorded_at": int(time.time()),
    }
