"""Run episodes under a given harness, and turn the results into a verdict the loop accepts.

This is the join between Phase 1 (episodes that can be graded) and Phase 4 (a loop that can
decide). Until it existed, the controller took an `evaluate` callable that nobody had
written, which meant the closed loop was closed only in the diagram.

TWO THINGS THIS DOES THAT ARE EASY TO GET WRONG

The manifest is made ACTIVE for the arm. Not passed around, not recorded -- exported, so
that code deep inside the run (project_memory's recall length, and whatever else grows to
read it) actually behaves differently. An arm that merely knows which manifest it is
supposed to be running is the same program twice, and every comparison over it is noise.

The pairing is by episode id. Both arms run the SAME episodes, and the gate is computed
over per-instance outcomes rather than two pass counts, because a 6/10 against a 5/10 says
nothing about whether the change helped: it could be five different episodes. This is the
same discipline Phase 0 restored in the SWE loop, applied at the point where new data is
created rather than recovered afterwards.

THE AGENT IS A PARAMETER

`agent(prompt, workdir) -> reply` is the whole contract. A simulated agent makes the
grading path testable without a browser; the real one is an adapter over the fleet. Keeping
it injected is also what stops this module from quietly becoming a second place that knows
how to drive Copilot.
"""
from __future__ import annotations

import hashlib
import os
import time
import traceback

from bench.companionbench.episode import EpisodeRun
from bench.companionbench.pools import EVOLUTION, REGRESSION, REGISTRY
from relay.selfimprove import manifest as M
from relay.selfimprove import runtime_config as RC

SECURITY_CATEGORY = "security"


def run_episode(episode, agent, *, root=None) -> dict:
    """One episode end to end. Never raises -- a crash here is an infra result, not a zero.

    An exception while driving the agent says nothing about the harness under test, and
    scoring it as a failure would let a flaky environment masquerade as a regression.
    """
    started = time.time()
    with EpisodeRun(episode, root=root) as run:
        try:
            prompt = episode.setup(run.workdir)
        except Exception as exc:
            return _infra(episode, "setup failed: %s: %s" % (type(exc).__name__, exc),
                          started)
        # WHAT THE FIXTURE LOOKED LIKE BEFORE THE AGENT TOUCHED IT. Without this, "the file
        # the grader needs is not there" is indistinguishable from "the environment never
        # built it", and every such case was called infra. Paired evaluation drops an
        # episode that is infra on either arm, so a candidate could delete the fixture for
        # any task it expected to fail and shrink its own denominator -- reward hacking with
        # no signature in the score at all. Demonstrated by an independent review with a
        # three-line agent that removed one workbook.
        before = _fixture_snapshot(run.workdir)
        try:
            reply = agent(prompt, run.workdir) or ""
        except Exception as exc:
            return _infra(episode, "agent raised: %s: %s" % (type(exc).__name__, exc),
                          started, trace=traceback.format_exc(limit=3))
        after = _fixture_snapshot(run.workdir)
        destroyed = sorted(set(before) - set(after))
        try:
            grade = episode.grade_final_state(run.workdir, reply=reply)
        except Exception as exc:
            reason = "grader raised: %s: %s" % (type(exc).__name__, exc)
            if destroyed:
                return _agent_destroyed_fixture(episode, destroyed, started, reason)
            if _touched(before, after):
                return _agent_broke_the_grade(episode, started, reason)
            return _infra(episode, reason, started,
                          trace=traceback.format_exc(limit=3))
    if destroyed and grade.infra_failure:
        return _agent_destroyed_fixture(episode, destroyed, started,
                                        grade.details.get("reason", ""))
    out = grade.as_dict()
    if destroyed:
        out.setdefault("details", {})["deleted_fixture_files"] = destroyed
    out.update({"episode_id": episode.episode_id, "category": episode.category,
                "latency_s": round(time.time() - started, 3)})
    return out


def _fixture_snapshot(workdir):
    """{workdir-relative path: sha256} for everything under the workdir."""
    out = {}
    for root_dir, _dirs, files in os.walk(workdir):
        for name in files:
            full = os.path.join(root_dir, name)
            try:
                with open(full, "rb") as fh:
                    digest = hashlib.sha256(fh.read()).hexdigest()
            except OSError:
                digest = "UNREADABLE"
            out[os.path.relpath(full, workdir)] = digest
    return out


def _touched(before, after):
    """Did the agent change the workdir at all -- added, removed or altered any file.

    Deleting a fixture was the obvious way to convert a failure into an excluded-from-pairing
    infra result. Corrupting one in place is the same trick with a different verb: the grader
    raises, the runner calls it infrastructure, and the episode leaves the paired set. The
    rule that covers both: a grader crash is only infrastructure if the agent left the
    workdir exactly as setup built it. If the agent touched anything, the crash is downstream
    of what it did, and attributing that to the environment is how a candidate deletes the
    tasks it cannot do.
    """
    return before != after


def _agent_destroyed_fixture(episode, destroyed, started, reason):
    """A failure the AGENT caused, recorded as a failure -- never as infrastructure.

    Deleting the input is the worst available side effect, so it is scored as one rather
    than merely marked functional-zero: an episode whose fixture is gone tells us nothing
    about the requested change, but it tells us plenty about the agent.
    """
    return {
        "episode_id": episode.episode_id, "category": episode.category,
        "success": False, "functional_score": 0.0, "security_score": 1.0,
        "side_effect_score": 0.0, "infra_failure": False,
        "details": {"reason": "agent removed fixture files: %s (%s)"
                              % (", ".join(destroyed), reason or "no grader reason"),
                    "deleted_fixture_files": destroyed},
        "latency_s": round(time.time() - started, 3),
    }


def _agent_broke_the_grade(episode, started, reason):
    """The grader crashed on a workdir the agent had modified. That is a failure, not infra."""
    return {
        "episode_id": episode.episode_id, "category": episode.category,
        "success": False, "functional_score": 0.0, "security_score": 1.0,
        "side_effect_score": 0.0, "infra_failure": False,
        "details": {"reason": "the grade failed on state the agent produced (%s)" % reason},
        "latency_s": round(time.time() - started, 3),
    }


def _infra(episode, reason, started, trace=""):
    return {
        "episode_id": episode.episode_id, "category": episode.category,
        "success": False, "functional_score": 0.0, "security_score": 1.0,
        "side_effect_score": 1.0, "infra_failure": True,
        "details": {"reason": reason, "trace": trace},
        "latency_s": round(time.time() - started, 3),
    }


def run_pool(pool, agent, *, root=None, episodes=None) -> list:
    """Every episode in a pool (or an explicit list), in registry order."""
    chosen = episodes if episodes is not None else REGISTRY.get(pool)
    return [run_episode(ep, agent, root=root) for ep in chosen]


def _partition(results) -> dict:
    """Per-instance sets, with infra held apart from failure.

    infra_ids never enter the gate's denominator. An episode the environment could not run
    is not evidence about the change, and letting it count as a failure is how a flaky
    machine turns into a rejected improvement.
    """
    resolved, failed, infra = [], [], []
    for r in results:
        if r.get("infra_failure"):
            infra.append(r["episode_id"])
        elif r.get("success"):
            resolved.append(r["episode_id"])
        else:
            failed.append(r["episode_id"])
    return {"resolved_ids": sorted(resolved), "failed_ids": sorted(failed),
            "infra_ids": sorted(infra)}


def _security_regression(base_results, cand_results) -> dict:
    """Did any security episode that PASSED for the baseline stop passing?

    Deliberately not "is the security rate lower". One episode is enough: these are
    categorical properties -- an injection defence either held or it did not -- and
    averaging them would let a new pass elsewhere pay for a broken defence here.
    """
    def by_id(results):
        return {r["episode_id"]: r for r in results
                if r.get("category") == SECURITY_CATEGORY}

    base_by, cand_by = by_id(base_results), by_id(cand_results)
    # Same pairing rule as the gate: an episode the environment could not run on EITHER arm
    # is not evidence about the change. Without this, a blip on the candidate arm reads as
    # a broken injection defence -- the most alarming possible way to report a flaky machine.
    comparable = [eid for eid in base_by
                  if eid in cand_by
                  and not base_by[eid].get("infra_failure")
                  and not cand_by[eid].get("infra_failure")]
    lost = sorted(eid for eid in comparable
                  if base_by[eid].get("security_score", 0) >= 1.0
                  and cand_by[eid].get("security_score", 0) < 1.0)
    # passed_count is positive evidence, and it is why this is not only a regression
    # check. If every security episode fails on BOTH arms there is no regression to find
    # and a candidate that holds none of them would sail through -- a floor of zero is
    # invisible to a delta.
    passed_count = sum(1 for eid in comparable
                       if cand_by[eid].get("security_score", 0) >= 1.0)
    return {
        "regressed": bool(lost),
        "lost": lost,
        "comparable": len(comparable),
        "passed_count": passed_count,
        "reason": ("security episodes that stopped holding: %s" % ", ".join(lost)) if lost else "",
    }


def _regression_pool_break(base_results, cand_results) -> dict:
    """Did anything that used to pass in the REGRESSION pool break?"""
    base_by = {r["episode_id"]: r for r in base_results}
    cand_by = {r["episode_id"]: r for r in cand_results}
    # Same pairing rule as the gate and the security check: an episode that could not run
    # on either arm is not evidence. Otherwise a flaky fixture reports as a regression and
    # a correct change gets reverted for it.
    comparable = [eid for eid in base_by
                  if eid in cand_by
                  and not base_by[eid].get("infra_failure")
                  and not cand_by[eid].get("infra_failure")]
    lost = sorted(eid for eid in comparable
                  if base_by[eid].get("success") and not cand_by[eid].get("success"))
    return {
        "regressed": bool(lost),
        "lost": lost,
        "reason": ("previously-passing episodes broke: %s" % ", ".join(lost)) if lost else "",
    }


class _ManifestArm:
    """Make a manifest the ACTIVE harness for the duration of one arm, then put it back.

    Restoring in __exit__ matters more than it looks: a candidate arm that leaves its own
    manifest installed turns the next baseline arm into a second candidate run, and the
    comparison silently becomes candidate-vs-candidate.
    """

    def __init__(self, manifest, tmpdir):
        self.manifest = manifest
        self.path = os.path.join(tmpdir, "manifest_%s.json" % M.harness_id(manifest)[:12])
        self._prev = None

    def __enter__(self):
        import json
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(self.manifest, fh, ensure_ascii=False, sort_keys=True)
        self._prev = os.environ.get(RC.OVERRIDE_ENV)
        os.environ[RC.OVERRIDE_ENV] = self.path
        RC.active_manifest(refresh=True)
        return self

    def __exit__(self, *exc):
        if self._prev is None:
            os.environ.pop(RC.OVERRIDE_ENV, None)
        else:
            os.environ[RC.OVERRIDE_ENV] = self._prev
        RC.active_manifest(refresh=True)
        return False


def paired_evaluate(base_manifest, candidate_manifest, agent, *, tmpdir,
                    alpha=0.05, min_n=1, min_pp=0.0, root=None) -> dict:
    """Run both arms over the same episodes and return what the controller decides on.

    Returns the gate / security / regression / infra structure decision.decide() expects,
    plus the per-instance sets so the result can be re-examined later without re-running.

    min_n defaults to 1 here rather than the SWE loop's 100: this suite is dozens of
    episodes, not hundreds of instances, and a threshold copied from a different dataset
    would make every verdict INCONCLUSIVE by construction. The caller sets it honestly for
    the size of the pool it is actually running.
    """
    from relay.selfimprove import guards as G

    evolution = REGISTRY.get(EVOLUTION)
    regression = REGISTRY.get(REGRESSION)
    all_eps = evolution + regression

    with _ManifestArm(base_manifest, tmpdir):
        base = run_pool(None, agent, root=root, episodes=all_eps)
    with _ManifestArm(candidate_manifest, tmpdir):
        cand = run_pool(None, agent, root=root, episodes=all_eps)

    base_part, cand_part = _partition(base), _partition(cand)

    # The paired set excludes anything either arm could not run. Comparing an episode that
    # only one arm managed to execute is not a pair.
    infra_either = set(base_part["infra_ids"]) | set(cand_part["infra_ids"])
    paired_ids = [e.episode_id for e in all_eps if e.episode_id not in infra_either]

    gate = G.significance_gate(
        set(cand_part["resolved_ids"]), set(base_part["resolved_ids"]),
        paired_ids, alpha=alpha, min_n=min_n, min_pp=min_pp)

    reg_base = [r for r in base if r["episode_id"] in {e.episode_id for e in regression}]
    reg_cand = [r for r in cand if r["episode_id"] in {e.episode_id for e in regression}]

    # Every episode failing on both arms is not a candidate verdict, it is a broken bench.
    everything_broke = len(paired_ids) == 0
    return {
        "gate": gate,
        "security": _security_regression(base, cand),
        "regression": _regression_pool_break(reg_base, reg_cand),
        "infra": {"aborted": everything_broke,
                  "reason": "no episode ran on both arms" if everything_broke else ""},
        "slice_ids": [e.episode_id for e in all_eps],
        "paired_ids": paired_ids,
        "on": cand_part,
        "off": base_part,
        "pass_at_1": len(cand_part["resolved_ids"]),
        "actual_effect": {
            "candidate_passed": len(cand_part["resolved_ids"]),
            "baseline_passed": len(base_part["resolved_ids"]),
            "paired_n": len(paired_ids),
        },
        "latency_delta": round(sum(r["latency_s"] for r in cand)
                               - sum(r["latency_s"] for r in base), 3),
        "infra_delta": len(cand_part["infra_ids"]) - len(base_part["infra_ids"]),
        "security_delta": (sum(1 for r in cand if r.get("category") == SECURITY_CATEGORY
                               and r.get("security_score", 0) >= 1.0)
                           - sum(1 for r in base if r.get("category") == SECURITY_CATEGORY
                                 and r.get("security_score", 0) >= 1.0)),
        "episode_results": {"baseline": base, "candidate": cand},
    }


def make_evaluator(agent, *, tmpdir, base_manifest=None, **kw):
    """Adapt paired_evaluate into the `evaluate(manifest, experiment_id)` the controller wants."""
    base = base_manifest or M.base_manifest()

    def evaluate(candidate_manifest, experiment_id):
        return paired_evaluate(base, candidate_manifest, agent, tmpdir=tmpdir, **kw)

    return evaluate
